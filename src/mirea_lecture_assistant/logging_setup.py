from __future__ import annotations

import faulthandler
import logging
import platform
import re
import sys
import threading
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__

EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
# Any UUID shape: attendance tokens are not guaranteed to be RFC 4122 v1-v5.
UUID = re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*\b")
JSON_SECRET = re.compile(
    r"""(?i)(["'](?:access_token|refresh_token|password|otp|emailCode)["']\s*:\s*["'])([^"']+)"""
)
SECRET_FIELD = re.compile(
    r"(?i)(password|пароль|token|токен|otp|emailcode|код подтверждения|код)(\s*[:=]\s*)(\S+)"
)
UNQUOTED_JSON_SECRET = re.compile(
    r"(?i)(\b(?:access_token|refresh_token|password|otp|emailCode)\s*:\s*)([^,}\s]+)"
)
AUTH_HEADER = re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+|basic\s+)?\S+")
COOKIE_HEADER = re.compile(r"(?i)((?:set-)?cookie\s*[:=]\s*)[^\n]+")
URL_PARAMETER = re.compile(r"([?&#][^=\s&#]+)=([^&\s#]+)")


def redact(text: str) -> str:
    text = EMAIL.sub("<email>", text)
    text = UUID.sub("<uuid>", text)
    text = JWT.sub("<jwt>", text)
    text = AUTH_HEADER.sub(r"\1<hidden>", text)
    text = COOKIE_HEADER.sub(r"\1<hidden>", text)
    text = JSON_SECRET.sub(r"\1<hidden>", text)
    text = UNQUOTED_JSON_SECRET.sub(r"\1<hidden>", text)
    text = SECRET_FIELD.sub(r"\1\2<hidden>", text)
    return URL_PARAMETER.sub(r"\1=<hidden>", text)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def _build_stamp() -> str:
    """When this exact binary was produced.

    Without it the journal cannot tell which build produced an entry, and a stale
    executable looks exactly like a bug in the current source.
    """
    target = Path(sys.executable if getattr(sys, "frozen", False) else __file__)
    try:
        stamp = datetime.fromtimestamp(target.stat().st_mtime).astimezone()
        return stamp.strftime("%d.%m.%Y %H:%M")
    except OSError:
        return "unknown"


_crash_file = None


def _record_hard_crashes(log_dir: Path) -> None:
    """Python stacks of a crash inside Qt or another native library.

    Such a crash ends the process before any Python handler runs; without this
    the journal just stops. The windowed executable has no stderr for it.
    """
    global _crash_file
    if _crash_file is not None:
        return
    try:
        _crash_file = open(log_dir / "crash.log", "a", encoding="utf-8")  # noqa: SIM115
        _crash_file.write(
            f"--- start {datetime.now().astimezone():%Y-%m-%d %H:%M:%S} v{__version__} "
            "(Windows lists handled exceptions here too: a crash is only where app.log stops)\n"
        )
        _crash_file.flush()
        faulthandler.enable(file=_crash_file, all_threads=True)
    except (OSError, RuntimeError):
        logging.getLogger("app").warning("crash_recorder_unavailable", exc_info=True)


def configure_logging(log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "app.log"
    handler = RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(
        RedactingFormatter(
            "%(asctime)s.%(msecs)03d %(levelname)s %(threadName)s %(name)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(handler)
    # pymirea logs final SSO URLs and page excerpts at INFO on every login.
    logging.getLogger("pymirea").setLevel(logging.WARNING)
    logging.captureWarnings(True)

    def exception_hook(exc_type, exc_value, traceback):
        logging.getLogger("crash").critical(
            "unhandled_exception",
            exc_info=(exc_type, exc_value, traceback),
        )

    def thread_exception_hook(args):
        if args.exc_type is SystemExit:
            return
        logging.getLogger("crash").critical(
            "unhandled_thread_exception thread=%s",
            getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = exception_hook
    threading.excepthook = thread_exception_hook
    _record_hard_crashes(log_dir)
    logging.getLogger("app").info(
        "application_start version=%s built=%s platform=%s python=%s frozen=%s",
        __version__,
        _build_stamp(),
        platform.platform(),
        platform.python_version(),
        bool(getattr(sys, "frozen", False)),
    )
    return log_path
