from __future__ import annotations

import logging
import platform
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
UUID = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
)
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*\b")
JSON_SECRET = re.compile(
    r"""(?i)(["'](?:access_token|refresh_token|password|otp|emailCode)["']\s*:\s*["'])([^"']+)"""
)
SECRET_FIELD = re.compile(
    r"(?i)(password|пароль|token|токен|otp|код подтверждения)(\s*[:=]\s*)(\S+)"
)
URL_PARAMETER = re.compile(r"([?&][^=\s&]+)=([^&\s]+)")


def redact(text: str) -> str:
    text = EMAIL.sub("<email>", text)
    text = UUID.sub("<uuid>", text)
    text = JWT.sub("<jwt>", text)
    text = JSON_SECRET.sub(r"\1<hidden>", text)
    text = SECRET_FIELD.sub(r"\1\2<hidden>", text)
    return URL_PARAMETER.sub(r"\1=<hidden>", text)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


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
    logging.getLogger("pymirea.session").setLevel(logging.WARNING)
    logging.captureWarnings(True)

    def exception_hook(exc_type, exc_value, traceback):
        logging.getLogger("crash").critical(
            "unhandled_exception",
            exc_info=(exc_type, exc_value, traceback),
        )

    sys.excepthook = exception_hook
    logging.getLogger("app").info(
        "application_start platform=%s python=%s frozen=%s",
        platform.platform(),
        platform.python_version(),
        bool(getattr(sys, "frozen", False)),
    )
    return log_path
