from __future__ import annotations

import logging
import os
import secrets
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from functools import partial
from string import Template

from PySide6.QtCore import (
    QEvent,
    QObject,
    QRunnable,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QDesktopServices,
    QGuiApplication,
    QKeySequence,
    QPalette,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import __version__, autostart, leftovers, manual_code, mcp_install, updater
from .async_runtime import run_async
from .browser_service import BrowserService, NotSignedInError
from .browser_warning import dismiss_password_notice
from .chat_detection import chat_baseline, classmates_report_attendance_issue
from .code_bridge import CodeBridge
from .database import Database
from .domain import Lesson, PendingAttendance, RuleMode, SessionState
from .email_otp import EMAIL_PROVIDERS, EmailAccount, ImapOtpReader
from .mcp_access import SETTINGS as MCP_SETTINGS
from .mcp_access import McpAccess, validate_settings
from .mirea_service import MireaService
from .moodle import discover_course_urls, is_group_code, resolve_lecture_url
from .moodle_login import SignInFailed, sign_in
from .qr import QrDeduplicator, ScreenScanner, validate_qr
from .relative_time import format_relative_time
from .reliability import (
    attendance_failure_counts_for_chat,
    should_retry_login,
    transient_login_failure,
)
from .security import SessionStore

log = logging.getLogger(__name__)

# A webinar lookup reads up to 30 СДО pages; repeating it every minute is wasteful.
LOOKUP_RETRY_EMPTY_SECONDS = 120
LOOKUP_RETRY_FAILED_SECONDS = 300
# After a failed СДО sign-in, wait before spending another emailed code on it.
SDO_SIGN_IN_BACKOFF_SECONDS = 600
# Rotating QR tokens are short-lived; retrying a stale one only produces noise.
ATTENDANCE_RETRY_WINDOW = timedelta(minutes=10)
# Rejections must go on this long before the public chat message is sent.
ATTENDANCE_FAILURE_SPAN_SECONDS = 120
# The pair is left only this long after its scheduled end. A room that says it
# is over before then was the wrong room, or the teacher closed it and is about
# to open another one.
LEAVE_AFTER_END = timedelta(minutes=5)
SETTINGS_PAGE = 3
# Room names Pulse gives online pairs; any other room is a real classroom.
ONLINE_ROOM_MARKERS = ("online", "онлайн", "дистан", "сдо", "вебинар", "mts", "мтс")
# A code letter that arrives this long after one of the app's own sign-ins is
# still taken for its own; only later ones are the student's.
OWN_CODE_GRACE_SECONDS = 120
# A copied code is taken off the clipboard again after this long.
CODE_CLIPBOARD_SECONDS = 90
# A frozen stream or failing captures may come from the stream or the computer,
# not from the tab: a reload is tried, but not again and again (each one leaves
# and re-enters the room, and a QR shown meanwhile is missed).
SOFT_RECOVERY_EVERY_SECONDS = 10 * 60
# While a pair runs, the СДО is checked again for a newer room of this group:
# often at the start, when teachers recreate rooms, rarely later.
RECHECK_EARLY_WINDOW = timedelta(minutes=20)
RECHECK_EARLY_SECONDS = 120
RECHECK_LATE_SECONDS = 300
# After a room closed early, the replacement is looked for every minute.
ROOM_LOST_RECHECK_SECONDS = 60
# How long such a room is left out of the search. A teacher may restart a
# session behind the same link, so it becomes eligible again afterwards.
REJECTED_ROOM_SECONDS = 300
# A session Pulse refuses minutes after it was issued is not "expired": logging in
# again would only email another code into the same refusal.
FRESH_SESSION_SECONDS = 600
# Later attempts of an automatic login that failed before any code was requested.
AUTO_LOGIN_RETRY_MINUTES = (2, 5, 15)
# «Открыть» this long before a pair still means "this pair": it is monitored at once.
MANUAL_OPEN_LEAD = timedelta(minutes=60)
# How often the cached schedule is checked for a pair to open, with or without Pulse.
LESSON_CHECK_MS = 15_000
# New releases are looked for this often, and the first time soon after start.
UPDATE_CHECK_MS = 6 * 60 * 60 * 1000
FIRST_UPDATE_CHECK_MS = 20_000
# When nothing else helps: after this long without a schedule (with the internet
# up), sign out of MIREA completely and sign in again; at most this often.
HARD_RELOGIN_AFTER_SECONDS = 20 * 60
HARD_RELOGIN_EVERY_SECONDS = 2 * 60 * 60
# Failures that mean "no connection" (see mirea_service.failure_reason), which a
# new login cannot fix, and the circuit breaker that follows them.
NETWORK_TROUBLE = (
    "не ответил вовремя",
    "не находится",
    "не удалось подключиться",
    "оборвал соединение",
    "прокси",
    "временно недоступна",
)


def is_network_trouble(message: str) -> bool:
    if any(part in message for part in NETWORK_TROUBLE):
        return True
    # "не отвечает" with no reason found: nothing suggests the login is at fault.
    return "не отвечает" in message and "Причина" not in message


# The chat fallback: a few spaced attempts per pair, not one per scanned frame.
CHAT_MAX_ATTEMPTS = 3
CHAT_RETRY_SECONDS = 60


class WheelGuard(QObject):
    """The mouse wheel scrolls the page, never the list or number under the pointer.

    Scrolling through the settings used to switch whatever list happened to pass
    under the pointer. A list still changes by clicking it, a number by typing or
    its arrows; an open list's popup scrolls as usual.
    """

    def eventFilter(self, watched, event):
        if event.type() != QEvent.Type.Wheel or not isinstance(
            watched, (QComboBox, QAbstractSpinBox)
        ):
            return False
        area = watched.parentWidget()
        while area is not None and not isinstance(area, QAbstractScrollArea):
            area = area.parentWidget()
        if area is not None:
            QApplication.sendEvent(area.viewport(), event)
        return True

    def guard(self, root: QWidget) -> None:
        for widget in (*root.findChildren(QComboBox), *root.findChildren(QAbstractSpinBox)):
            widget.installEventFilter(self)


def _fill_email_providers(combo: QComboBox, selected: str = "auto") -> None:
    for provider, (label, _host, _port) in EMAIL_PROVIDERS.items():
        combo.addItem(label, provider)
    index = combo.findData(selected)
    combo.setCurrentIndex(max(index, 0))


def _update_imap_fields(combo: QComboBox, host: QLineEdit, port: QSpinBox, hint: QLabel) -> None:
    provider = combo.currentData() or "auto"
    _label, preset_host, preset_port = EMAIL_PROVIDERS[provider]
    custom = provider == "custom"
    host.setEnabled(custom)
    port.setEnabled(custom)
    if preset_host:
        host.setText(preset_host)
        port.setValue(preset_port)
    elif provider == "auto":
        host.clear()
        port.setValue(993)
        host.setPlaceholderText("Определится по адресу")
    messages = {
        "auto": "Gmail, Яндекс, Mail.ru, Рамблер и Outlook определяются по адресу. Для другой почты выберите ручной IMAP.",
        "gmail": 'Нужен <a href="https://myaccount.google.com/apppasswords">пароль приложения Google</a>.',
        "yandex": 'Включите IMAP и создайте <a href="https://id.yandex.ru/security/app-passwords">пароль приложения для Почты</a>. Вводить нужно сам пароль, не его название. Если адрес — алиас, укажите основной логин Яндекс ID в поле «Логин IMAP».',
        "mailru": 'Создайте <a href="https://help.mail.ru/mail/login/mailer/">пароль для внешнего приложения Mail.ru</a>.',
        "rambler": "В настройках Рамблер Почты включите доступ для почтовых программ (IMAP) и укажите пароль от почты.",
        "microsoft": "Microsoft требует OAuth2; пароль сработает только для аккаунтов, где разрешён пароль приложения. Иначе код нужно ввести вручную.",
        "custom": "Укажите SSL/TLS IMAP-сервер и порт своего почтового провайдера.",
    }
    hint.setText(messages[provider])


def _email_account_from_fields(
    address: str,
    password: str,
    provider: str,
    host: str,
    port: int,
    username: str = "",
) -> EmailAccount:
    return EmailAccount(address, password, provider, host, port, username).normalized()


# Colour tokens of the two themes; the stylesheet and every coloured cell use them.
THEMES = {
    "light": {
        "bg": "#f5f7fb",
        "surface": "#ffffff",
        "text": "#172033",
        "title": "#111827",
        "muted": "#64748b",
        "border": "#e4e9f2",
        "input_border": "#d8deea",
        "header": "#f8fafc",
        "grid": "#eef1f6",
        "accent": "#3451d1",
        "accent_hover": "#2943b3",
        "on_accent": "#ffffff",
        "secondary_bg": "#e8edff",
        "secondary_fg": "#2943b3",
        "disabled_bg": "#cbd5e1",
        "disabled_fg": "#475569",
        "input_disabled": "#f1f5f9",
        "spin_button": "#eef2ff",
        "focus": "#93c5fd",
        "sidebar": "#172554",
        "sidebar_hover": "#24346b",
        "sidebar_text": "#cbd5e1",
        "ok": "#15803d",
        "info": "#1d4ed8",
        "warn": "#b45309",
        "error": "#b91c1c",
        "past": "#94a3b8",
        "current": "#e0e7ff",
        "selection": "#c7d2fe",
        "mode_auto": "#dcfce7",
        "mode_ask": "#fef9c3",
        "mode_ignore": "#f1f5f9",
    },
    "dark": {
        "bg": "#0f1420",
        "surface": "#182031",
        "text": "#e5e9f2",
        "title": "#f3f5fa",
        "muted": "#94a3b8",
        "border": "#2a3447",
        "input_border": "#3a4660",
        "header": "#1d2638",
        "grid": "#243047",
        "accent": "#5b76f0",
        "accent_hover": "#4a64dd",
        "on_accent": "#ffffff",
        "secondary_bg": "#26315c",
        "secondary_fg": "#c7d2fe",
        "disabled_bg": "#334155",
        "disabled_fg": "#94a3b8",
        "input_disabled": "#1b2333",
        "spin_button": "#26315c",
        "focus": "#93c5fd",
        "sidebar": "#0b1226",
        "sidebar_hover": "#1a2750",
        "sidebar_text": "#cbd5e1",
        "ok": "#4ade80",
        "info": "#93c5fd",
        "warn": "#fbbf24",
        "error": "#f87171",
        "past": "#64748b",
        "current": "#26315c",
        "selection": "#34427a",
        "mode_auto": "#14532d",
        "mode_ask": "#713f12",
        "mode_ignore": "#1f2937",
    },
}
THEME_CHOICES = (("system", "Как в системе"), ("light", "Светлая"), ("dark", "Тёмная"))

STYLE = Template("""
QMainWindow, QWidget { background: $bg; color: $text; font-family: 'Segoe UI'; font-size: 14px; }
QLabel { background: transparent; }
QToolTip { background: $surface; color: $text; border: 1px solid $border; padding: 4px; }
QMenu { background: $surface; color: $text; border: 1px solid $border; }
QMenu::item:selected { background: $selection; }
QMenu::item:disabled { color: $muted; }
#sidebar { background: $sidebar; min-width: 210px; max-width: 210px; }
#brand { color: white; font-size: 17px; font-weight: 700; padding: 20px 14px; }
#nav { text-align: left; color: $sidebar_text; border: 0; border-radius: 8px; padding: 11px 16px; margin: 2px 10px; background: transparent; }
#nav:hover { background: $sidebar_hover; color: white; }
#nav:checked { background: $accent; color: white; font-weight: 600; }
#nav:focus { border: 2px solid $focus; }
#pageTitle { font-size: 27px; font-weight: 700; color: $title; }
#muted { color: $muted; }
#card { background: $surface; border: 1px solid $border; border-radius: 12px; padding: 14px; }
#nowCard { background: $surface; border: 1px solid $border; border-left: 4px solid $accent; border-radius: 10px; padding: 12px 16px; font-size: 15px; }
#dirty { color: $warn; font-weight: 600; }
#activity { color: $accent; padding: 0 8px; }
QGroupBox { color: $text; }
QGroupBox#section { background: $surface; border: 1px solid $border; border-radius: 12px; margin-top: 22px; padding: 16px 14px 10px 14px; font-weight: 600; }
QGroupBox#section::title { subcontrol-origin: margin; left: 12px; padding: 0 4px; color: $accent; }
QScrollArea { border: 0; background: transparent; }
QPushButton { background: $accent; color: $on_accent; border: 0; border-radius: 7px; padding: 9px 15px; font-weight: 600; }
QPushButton:hover { background: $accent_hover; }
QPushButton:focus { border: 2px solid $focus; }
QPushButton:disabled { background: $disabled_bg; color: $disabled_fg; }
QPushButton#secondary { background: $secondary_bg; color: $secondary_fg; }
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit { background: $surface; color: $text; border: 1px solid $input_border; border-radius: 7px; padding: 8px; selection-background-color: $selection; }
QComboBox QAbstractItemView { background: $surface; color: $text; selection-background-color: $selection; }
QSpinBox { padding-right: 22px; min-width: 90px; }
QSpinBox::up-button, QSpinBox::down-button { width: 20px; border: 0; background: $spin_button; }
QSpinBox::up-button { border-top-right-radius: 7px; }
QSpinBox::down-button { border-bottom-right-radius: 7px; }
QCheckBox { color: $text; background: transparent; spacing: 8px; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid $input_border; border-radius: 4px; background: $surface; }
QCheckBox::indicator:checked { background: $accent; border: 1px solid $accent; }
QCheckBox::indicator:focus { border: 1px solid $focus; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: $input_border; border-radius: 4px; min-height: 28px; }
QScrollBar::handle:horizontal { background: $input_border; border-radius: 4px; min-width: 28px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QTableWidget QPushButton { padding: 4px 10px; border-radius: 6px; }
QTableWidget QLineEdit { padding: 3px 6px; border-radius: 5px; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border: 1px solid $accent; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { background: $input_disabled; color: $muted; }
QTableWidget { background: $surface; color: $text; border: 1px solid $border; border-radius: 10px; gridline-color: $grid; selection-background-color: $selection; selection-color: $text; }
QHeaderView::section { background: $header; color: $text; border: 0; border-bottom: 1px solid $border; padding: 9px; font-weight: 600; }
QStatusBar { background: $bg; color: $text; }
""")

MODE_LABELS = {RuleMode.AUTO: "Авто", RuleMode.ASK: "Спрашивать", RuleMode.IGNORE: "Не открывать"}
MODE_TOKENS = {RuleMode.AUTO: "mode_auto", RuleMode.ASK: "mode_ask", RuleMode.IGNORE: "mode_ignore"}
HISTORY_STATUS = {
    "detected": ("Отправляется", "info"),
    "retrying": ("Повторная отправка", "warn"),
    "ignored": ("Пропущен", "muted"),
    "submitted": ("Посещение подтверждено", "ok"),
    "failed": ("Ошибка", "error"),
    "rejected": ("Отклонён", "error"),
    "invalid": ("Неверный QR", "error"),
}
AUTH_STATES = {
    "signed_in": ("● MIREA: вход выполнен", "#86efac"),
    "checking": ("● MIREA: входим…", "#93c5fd"),
    "expired": ("● MIREA: сессия истекла", "#fbbf24"),
    "signed_out": ("● MIREA: вход не выполнен", "#fbbf24"),
}


class WorkerSignals(QObject):
    done = Signal(object)
    failed = Signal(str)


class CodeSignals(QObject):
    """Carries a code from the mailbox watcher's thread to the window's."""

    arrived = Signal(str)
    entered = Signal()


class McpSignals(QObject):
    requested = Signal(object)


class Worker(QRunnable):
    def __init__(
        self,
        function,
        operation: str = "background_operation",
        *,
        log_success: bool = True,
    ):
        super().__init__()
        self.function = function
        self.operation = operation
        self.log_success = log_success
        self.operation_id = secrets.token_hex(4)
        self.signals = WorkerSignals()

    @Slot()
    def run(self):
        started = time.perf_counter()
        if self.log_success:
            log.info("operation_start id=%s name=%s", self.operation_id, self.operation)
        try:
            result = self.function()
        except Exception as exc:  # UI boundary: turn service errors into messages
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            log.exception(
                "operation_failed id=%s name=%s elapsed_ms=%s",
                self.operation_id,
                self.operation,
                elapsed_ms,
            )
            message = str(exc) or "Внутренняя ошибка"
            self.signals.failed.emit(f"{message}\n\nКод диагностики: {self.operation_id}")
        else:
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            if self.log_success:
                log.info(
                    "operation_done id=%s name=%s elapsed_ms=%s",
                    self.operation_id,
                    self.operation,
                    elapsed_ms,
                )
            self.signals.done.emit(result)


class SetupDialog(QDialog):
    """Asked once, on a fresh installation, before anything else happens."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Первый запуск")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Для работы понадобятся три вещи:<br>"
            "• учётная запись МИРЭА — приложение входит в «Пульс» и в СДО;<br>"
            "• почта, куда приходит код подтверждения, и <b>пароль приложения</b> для неё "
            "(обычный пароль от почты не подойдёт);<br>"
            "• ваша группа — по ней среди вебинаров выбирается нужная комната.<br><br>"
            "Всё хранится только на этом компьютере: пароли — в хранилище Windows."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        self.group = QLineEdit()
        self.group.setPlaceholderText("Например, ИКБО-01-24")
        self.student_name = QLineEdit()
        self.student_name.setPlaceholderText("Иванов Иван")
        form.addRow("Группа", self.group)
        form.addRow("Фамилия и имя", self.student_name)
        hint = QLabel("Фамилия и имя нужны только для резервного сообщения в чат лекции.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        form.addRow("", hint)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Дальше: вход в МИРЭА")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class LoginDialog(QDialog):
    def __init__(self, parent=None, email_credentials: EmailAccount | None = None):
        super().__init__(parent)
        self.setWindowTitle("Вход в MIREA")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Используйте университетскую учётную запись. Данные можно сохранить в защищённом хранилище Windows."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        self.username = QLineEdit()
        self.username.setPlaceholderText("student@edu.mirea.ru")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Логин", self.username)
        form.addRow("Пароль", self.password)
        self.email_provider = QComboBox()
        _fill_email_providers(
            self.email_provider, email_credentials.provider if email_credentials else "auto"
        )
        self.email_address = QLineEdit()
        self.email_address.setPlaceholderText("Почта, куда приходит код")
        self.email_password = QLineEdit()
        self.email_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.email_password.setPlaceholderText("Пароль приложения почты")
        self.imap_username = QLineEdit()
        self.imap_username.setPlaceholderText("Обычно определяется по адресу")
        self.imap_host = QLineEdit()
        self.imap_port = QSpinBox()
        self.imap_port.setRange(1, 65535)
        self.imap_port.setValue(993)
        if email_credentials:
            self.email_address.setText(email_credentials.address)
            self.email_password.setPlaceholderText("Уже сохранён")
            self.imap_host.setText(email_credentials.imap_host)
            self.imap_port.setValue(email_credentials.imap_port)
            self.imap_username.setText(email_credentials.imap_username)
        form.addRow("Провайдер почты", self.email_provider)
        form.addRow("Почта для кода", self.email_address)
        form.addRow("Пароль приложения", self.email_password)
        form.addRow("Логин IMAP", self.imap_username)
        form.addRow("IMAP-сервер", self.imap_host)
        form.addRow("Порт IMAP", self.imap_port)
        self.email_help = QLabel()
        self.email_help.setOpenExternalLinks(True)
        self.email_help.setWordWrap(True)
        form.addRow("", self.email_help)
        self.email_provider.currentIndexChanged.connect(
            lambda: _update_imap_fields(
                self.email_provider, self.imap_host, self.imap_port, self.email_help
            )
        )
        _update_imap_fields(self.email_provider, self.imap_host, self.imap_port, self.email_help)
        self.remember = QCheckBox("Сохранить данные и входить автоматически")
        self.remember.setChecked(True)
        form.addRow("", self.remember)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Войти")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class SourcesDialog(QDialog):
    """Pages of the СДО that are searched for this subject's webinars."""

    def __init__(self, subject: str, urls: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Источники вебинаров")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"<b>{subject}</b>"))
        hint = QLabel(
            "Обычно заполнять не нужно: приложение само находит курс предмета в СДО "
            "и раздел с вебинарами. Здесь адреса можно задать вручную, по одному в "
            "строке, если автопоиск ошибся. Вебинар выбирается по предмету, дате, "
            "времени и вашей группе."
        )
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        layout.addWidget(hint)
        self.editor = QPlainTextEdit("\n".join(urls))
        self.editor.setPlaceholderText("https://online-edu.mirea.ru/mod/webinars/view.php?id=…")
        layout.addWidget(self.editor)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Сохранить")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def urls(self) -> list[str]:
        return [line.strip() for line in self.editor.toPlainText().splitlines() if line.strip()]


class MainWindow(QMainWindow):
    # Until then a MIREA code letter belongs to the app's own sign-in (monotonic time).
    own_codes_until = 0.0
    _login_in_progress = False
    # The app's Pulse sign-in has asked for a code and not received it yet. Only
    # then is a new code letter its own: a sign-in retried for many minutes (Pulse
    # refusing the session) used to swallow the codes of the student's own logins.
    awaiting_own_code = False

    @property
    def login_in_progress(self) -> bool:
        return self._login_in_progress

    @login_in_progress.setter
    def login_in_progress(self, running: bool) -> None:
        if self._login_in_progress and not running and self.awaiting_own_code:
            # Ended without its code: that letter may still be on its way.
            self._claim_own_codes()
        if not running:
            self.awaiting_own_code = False
        self._login_in_progress = running

    def _own_code_taken(self, code: str) -> None:
        """The app has the code it asked for; later letters are the student's."""
        self.awaiting_own_code = False
        self.used_codes.add(code)

    def _claim_own_codes(self) -> None:
        self.own_codes_until = max(self.own_codes_until, time.monotonic() + OWN_CODE_GRACE_SECONDS)
        if getattr(self, "code_bridge", None):
            self.code_bridge.clear()

    def __init__(self, database: Database):
        super().__init__()
        self.db = database
        self.session_store = SessionStore()
        self.otp_reader = ImapOtpReader()
        self.code_watcher: manual_code.CodeWatcher | None = None
        # Codes the app received for its own sign-ins: never copied or typed.
        self.used_codes: set[str] = set()
        self.code_signals = CodeSignals()
        self.code_signals.arrived.connect(self._manual_code_arrived)
        self.code_signals.entered.connect(self._manual_code_entered)
        self.code_bridge = CodeBridge(self.code_signals.entered.emit)
        from .paths import data_dir

        self.mcp_signals = McpSignals()
        self.mcp_signals.requested.connect(self._mcp_rpc)
        self.mcp_access = McpAccess(data_dir(), self.mcp_signals.requested.emit)
        self.mcp_busy = False
        # Old MCP versions go once no AI client runs them, as the app's own .old does.
        self.mcp_leftovers = True
        try:
            session = self.session_store.load()
        except Exception:
            log.exception("keyring load failed")
            session = None
        self.mirea = MireaService(session)
        # What is on disk, to save pymirea's in-place token refreshes only when they happen.
        self.persisted_session = self._session_fingerprint(session or {})
        self.deduplicator = QrDeduplicator(database)
        self.scanner = ScreenScanner()
        from .paths import SHOW_REQUEST_FILE, SHOW_RESPONSE_FILE, data_dir

        self.browser = BrowserService(data_dir() / "browser-profile")
        self.pending_qr: dict[int, PendingAttendance] = {}
        self.session_store.discard_obsolete_pending_attendance()
        self.latest_qr_event_by_lesson: dict[str | None, int] = {}
        self.retry_attempts: dict[int, int] = {}
        self.attendance_failures_by_lesson: dict[str | None, int] = {}
        self.attendance_first_failure_at: dict[str | None, float] = {}
        self.attendance_inflight: set[int] = set()
        self.unreadable_chat_sent = False
        self.qr_detected_in_lecture = False
        self.chat_config_warned = False
        self.active_lecture_id: str | None = None
        self.active_lecture_url: str | None = None
        self.opening_lecture_id: str | None = None
        self.joined_lessons: set[str] = set()
        self.prompted_lessons: set[str] = set()
        self.resolving_lessons: set[str] = set()
        self.active_workers: set[Worker] = set()
        self.schedule_refresh_running = False
        self.pool = QThreadPool.globalInstance()
        # Code waits and СДО lookups hold threads for minutes; they must not
        # starve frame capture and attendance submission.
        self.pool.setMaxThreadCount(max(8, self.pool.maxThreadCount()))
        self.scan_pool = QThreadPool(self)
        self.scan_pool.setMaxThreadCount(2)
        self.active_lesson: Lesson | None = None
        self.retry_scheduled: set[int] = set()
        self.submit_attempts: dict[int, int] = {}
        self.lookup_not_before: dict[str, float] = {}
        # Lessons whose room closed before the pair was over: waiting for a new one.
        self.room_lost_lessons: set[str] = set()
        # СДО webinar ids of the rooms seen, to tell a newer room from an older one.
        self.room_webinar_ids: dict[str, int] = {}
        # (lesson, room) pairs already reported as closed: one notification each.
        self.room_lost_notified: set[tuple[str, str]] = set()
        # Chat lines already on screen when the lecture was joined, and when that was.
        self.chat_baseline: frozenset[str] | None = None
        self.lecture_started_at = 0.0
        self.sdo_sign_in_lock = threading.Lock()
        self.sdo_sign_in_failed_at: float | None = None
        self.schedule_redraw_pending = False
        self.schedule_signature = None
        self.busy_operations: dict[int, str] = {}
        self.scan_running = False
        self.scan_started_at = 0.0
        self.last_capture_heartbeat = 0.0
        self.last_capture_at = 0.0
        self.force_exit = False
        self.pending_login_credentials: tuple[str, str] | None = None
        self.pending_email_credentials: EmailAccount | None = None
        self.email_uid_before_login: int | None = None
        self.otp_submission_attempted = False
        self.login_cycle_retries = 0
        self.login_in_progress = False
        self.auth_recovery_running = False
        self.automatic_login_cycle = False
        self.login_retry_attempt = 0
        self.login_retry_scheduled = False
        self.login_retry_generation = 0
        self.session_recheck_scheduled = False
        self.pending_pulse_login = None
        self.remember_login_requested = False
        self.login_started_at = datetime.now(UTC)
        self.session_obtained_at: float | None = None
        # Whether the last СДО sign-in actually ran and worked (lookups waiting for it reuse it).
        self.sdo_last_sign_in_ok = False
        # Since when the schedule keeps failing, and when the app last signed out
        # and in again because of it.
        self.schedule_failing_since: float | None = None
        self.last_hard_relogin: float | None = None
        # ASK pairs the student agreed to open: retried like AUTO ones if opening fails.
        self.accepted_lessons: set[str] = set()
        self.chat_attempts: dict[str, tuple[int, float]] = {}

        self.setWindowTitle("MIREA Lecture Assistant")
        self.resize(1080, 700)
        self.setMinimumSize(880, 580)
        self.colors = THEMES["light"]
        self._build_ui()
        self._build_tray()
        self._load_settings()
        self._apply_theme()
        hints = QGuiApplication.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            # A bound method is disconnected with the window; a lambda outlived it.
            hints.colorSchemeChanged.connect(self._color_scheme_changed)
        self.refresh_views()
        log.info("main_window_ready")

        self.clock = QTimer(self)
        self.clock.timeout.connect(self._refresh_relative_times)
        self.clock.start(30_000)
        self.show_request = data_dir() / SHOW_REQUEST_FILE
        self.show_response = data_dir() / SHOW_RESPONSE_FILE
        self.show_request_timer = QTimer(self)
        self.show_request_timer.timeout.connect(self._check_show_request)
        self.show_request_timer.start(1_000)
        self.scan_timer = QTimer(self)
        self.scan_timer.timeout.connect(self._scan_tick)
        self.schedule_timer = QTimer(self)
        self.schedule_timer.setInterval(60_000)
        self.schedule_timer.timeout.connect(self._refresh_schedule_background)
        self.schedule_timer.start()
        self.lecture_watch_timer = QTimer(self)
        self.lecture_watch_timer.setInterval(10_000)
        self.lecture_watch_timer.timeout.connect(self._lecture_watch_tick)
        self.lecture_watch_timer.start()
        # Pairs are opened from the cached schedule even while Pulse is down or the
        # session is being renewed; this used to hang on a successful refresh.
        self.lesson_timer = QTimer(self)
        self.lesson_timer.setInterval(LESSON_CHECK_MS)
        self.lesson_timer.timeout.connect(self._evaluate_cached_lessons)
        self.lesson_timer.start()
        self.lecture_health_check_running = False
        self.entering_lecture_room = False
        self.lecture_recovery_failures = 0
        self.lecture_unstable_checks = 0
        self.lecture_inactive_checks = 0
        self.soft_recovery_at: float | None = None
        if os.environ.get("MIREA_ASSISTANT_SMOKE_TEST") != "1":
            QTimer.singleShot(0, self._restore_active_lecture)
        QTimer.singleShot(0, self._startup_auth)
        self._sync_autostart()
        self.available_update: updater.Release | None = None
        self.update_cleanup_timer = QTimer(self)
        self.update_cleanup_timer.setInterval(5_000)
        self.update_cleanup_timer.timeout.connect(self._cleanup_old_version)
        QTimer.singleShot(3_000, self._restart_code_watcher)
        QTimer.singleShot(0, self._start_code_bridge)
        # Unpacked copies left by killed runs (100+ MB each): once a minute after
        # start, when the start itself is done, then every six hours.
        self.leftovers_timer = QTimer(self)
        self.leftovers_timer.setInterval(6 * 60 * 60 * 1000)
        self.leftovers_timer.timeout.connect(self._clean_runtime_leftovers)
        if os.environ.get("MIREA_ASSISTANT_SMOKE_TEST") != "1":
            QTimer.singleShot(60_000, self._clean_runtime_leftovers)
            self.leftovers_timer.start()
        self.mcp_timer = QTimer(self)
        self.mcp_timer.timeout.connect(self._refresh_mcp_status)
        self.mcp_timer.start(5_000)
        QTimer.singleShot(0, self._sync_mcp_access)
        if updater.can_self_update() and os.environ.get("MIREA_ASSISTANT_SMOKE_TEST") != "1":
            # An update needs a check (20 s) and a click first, so no download of
            # this copy can be running yet: leftovers of an interrupted one go too.
            QTimer.singleShot(1_000, lambda: self._cleanup_old_version(partial=True))
        self.update_check_running = False
        self.update_timer = QTimer(self)
        self.update_timer.setInterval(UPDATE_CHECK_MS)
        self.update_timer.timeout.connect(self._check_for_updates)
        if updater.can_self_update() and os.environ.get("MIREA_ASSISTANT_SMOKE_TEST") != "1":
            self.update_timer.start()
            QTimer.singleShot(FIRST_UPDATE_CHECK_MS, self._check_for_updates)
        if self.db.recovery:
            QTimer.singleShot(0, self._report_database_recovery)

    def _clean_runtime_leftovers(self):
        threading.Thread(
            target=leftovers.clean_runtime_folders, name="runtime-leftovers", daemon=True
        ).start()

    def _start_code_bridge(self):
        from .paths import data_dir, resource_path

        if os.environ.get("MIREA_ASSISTANT_SMOKE_TEST") == "1":
            return
        try:
            self.code_bridge.start()
            self.code_bridge.prepare_extension(
                resource_path("browser_extension"), data_dir() / "browser-extension"
            )
        except OSError:
            log.warning("manual_code_bridge_unavailable")

    def _manual_code_entered(self):
        log.info("manual_code_delivered copied=True typed=True via=extension")
        self.tray.showMessage(
            "Код МИРЭА введён",
            "Код введён в поле почтового подтверждения в браузере",
            QSystemTrayIcon.MessageIcon.Information,
            15000,
        )

    def _restart_code_watcher(self):
        """Watch the mailbox for codes of the student's own sign-ins, if wanted."""
        self.code_bridge.clear()
        if self.code_watcher is not None:
            self.code_watcher.stop()
            self.code_watcher = None
        if os.environ.get("MIREA_ASSISTANT_SMOKE_TEST") == "1":
            return
        if not bool(self.db.get_setting("copy_manual_codes", True)):
            return
        try:
            account = self.session_store.load_email_credentials()
        except Exception:
            log.warning("manual_code_watcher_no_credentials", exc_info=True)
            return
        if account is None:
            return
        self.code_watcher = manual_code.CodeWatcher(account, self.code_signals.arrived.emit)
        self.code_watcher.start()

    def _manual_code_arrived(self, code: str):
        if code in self.used_codes or manual_code.own_code_window_is_open(
            self.own_codes_until,
            (self.login_in_progress and self.awaiting_own_code) or self.sdo_sign_in_lock.locked(),
        ):
            log.info("manual_code_ignored reason=app_sign_in")
            return
        if not bool(self.db.get_setting("copy_manual_codes", True)):
            return
        self._copy_code(code)
        # Best effort native Close/OK; never send Enter into an arbitrary window.
        if os.environ.get("MIREA_ASSISTANT_SMOKE_TEST") != "1":
            threading.Thread(target=dismiss_password_notice, daemon=True).start()
        typed = False
        window = manual_code.foreground_window()
        if window is not None and bool(self.db.get_setting("type_manual_codes", True)):
            title, window_class, pid, program = window
            ours = self.browser.process is not None and pid == self.browser.process.pid
            # Keyboard focus is not proof of the target field. The extension
            # fills an exact SSO email challenge, even in an inactive tab.
            # The title tells why a code was or was not typed; the code is never logged.
            log.info(
                "manual_code_window program=%s class=%s ours=%s title=%r",
                program,
                window_class,
                ours,
                title[:80],
            )
        if bool(self.db.get_setting("type_manual_codes", True)):
            self.code_bridge.publish(code)
        log.info("manual_code_delivered copied=True typed=%s", typed)
        self.tray.showMessage(
            "Код МИРЭА" + (" введён" if typed else " скопирован"),
            f"{code} — " + ("введён на странице входа" if typed else "вставьте его: Ctrl+V"),
            QSystemTrayIcon.MessageIcon.Information,
            15000,
        )

    def _copy_code(self, code: str):
        """Copy a one-time code, kept out of Windows' clipboard history and cloud sync."""
        clipboard = QGuiApplication.clipboard()
        if not manual_code.copy_secret(code):
            # Plain text only: a QMimeData left on the clipboard crashed Qt's shutdown.
            clipboard.setText(code)

        def forget():
            if clipboard.text() == code:
                clipboard.clear()

        QTimer.singleShot(CODE_CLIPBOARD_SECONDS * 1000, forget)

    def _cleanup_old_version(self, partial: bool = False):
        # Independent of update-check settings and of network availability.
        if updater.clean_leftovers(updater.current_executable(), partial=partial):
            self.update_cleanup_timer.stop()
        elif not self.update_cleanup_timer.isActive():
            self.update_cleanup_timer.start()

    def _check_for_updates(self, manual: bool = False):
        if self.update_check_running:
            return
        if not manual and not bool(self.db.get_setting("check_updates", True)):
            return
        if updater.can_self_update():
            updater.clean_leftovers(updater.current_executable())
        self.update_check_running = True

        def failed(message: str):
            self.update_check_running = False
            log.info("update_check_failed message=%s", message)
            if manual:
                QMessageBox.warning(
                    self, "Обновления", f"Не удалось проверить обновления: {message}"
                )

        self._run(
            updater.latest_release,
            lambda release: self._update_checked(release, manual),
            "Проверяем обновления…",
            failed=failed,
        )

    def _update_checked(self, release, manual: bool):
        self.update_check_running = False
        if release is None or not updater.is_newer(release.version, __version__):
            if manual:
                QMessageBox.information(
                    self, "Обновления", f"У вас последняя версия ({__version__})."
                )
            return
        self.available_update = release
        log.info("update_available version=%s", release.version)
        self.update_button.setText(f"Обновить до {release.version}")
        self.update_button.show()
        self.tray.showMessage(
            "Вышла новая версия",
            f"MIREA Lecture Assistant {release.version}: кнопка «Обновить» слева в окне.",
            QSystemTrayIcon.MessageIcon.Information,
            8000,
        )
        if manual:
            self._start_update()

    def _start_update(self):
        release = self.available_update
        if release is None:
            return
        if not updater.can_self_update():
            QDesktopServices.openUrl(QUrl(updater.RELEASES_PAGE))
            return
        notes = f"\n\nЧто нового:\n{release.notes}" if release.notes else ""
        if not self._ask(
            "Обновление",
            f"Установить версию {release.version} (сейчас {__version__})? Приложение "
            "перезапустится; расписание, вход, пароли и настройки сохранятся." + notes,
        ):
            return
        # Nothing to lose, but a fresh copy costs nothing either.
        self._persist_session()
        self.db.backup()
        self.update_button.setEnabled(False)
        self.update_button.setText("Скачиваем обновление…")
        folder = updater.current_executable().parent
        self._run(
            lambda: updater.download(release, folder),
            self._update_downloaded,
            "Скачиваем обновление…",
            failed=self._update_failed,
        )

    def _update_downloaded(self, new_exe):
        try:
            installed = updater.install(new_exe, updater.current_executable())
            updater.start(installed)
        except OSError as exc:
            log.warning("update_install_failed", exc_info=True)
            self._update_failed(
                f"Не удалось заменить файл программы ({exc}). Новая версия сохранена: "
                f"{new_exe} — закройте программу и запустите этот файл."
            )
            return
        # The new copy asks this one to step aside as soon as it starts.
        self.update_button.setText("Перезапускаем…")
        self.statusBar().showMessage("Обновление установлено, запускаем новую версию…", 10000)

    def _update_failed(self, message: str):
        log.warning("update_failed message=%s", message)
        self.update_button.setEnabled(True)
        if self.available_update is not None:
            self.update_button.setText(f"Обновить до {self.available_update.version}")
        QMessageBox.warning(
            self,
            "Обновление",
            f"{message}\n\nНовую версию можно скачать и вручную: {updater.RELEASES_PAGE}",
        )

    def _sync_autostart(self):
        """On by default; the registered path follows the program if it was moved."""
        if not autostart.available() or os.environ.get("MIREA_ASSISTANT_SMOKE_TEST") == "1":
            return
        enabled = bool(self.db.get_setting("autostart", True))
        self.db.set_setting("autostart", enabled)
        try:
            wanted = autostart.command(updater.current_executable())
            current = autostart.registered()
            if enabled and current != wanted:
                autostart.set_enabled(True, updater.current_executable())
            elif not enabled and current is not None:
                autostart.set_enabled(False, updater.current_executable())
        except OSError:
            log.warning("autostart_sync_failed", exc_info=True)

    def _autostart_toggled(self, checked: bool):
        self.db.set_setting("autostart", bool(checked))
        if not autostart.available():
            return
        try:
            autostart.set_enabled(bool(checked), updater.current_executable())
        except OSError as exc:
            log.warning("autostart_toggle_failed", exc_info=True)
            QMessageBox.warning(self, "Автозапуск", f"Не удалось изменить автозапуск: {exc}")
            return
        self.statusBar().showMessage(
            "Приложение будет запускаться вместе с Windows"
            if checked
            else "Автозапуск с Windows выключен",
            5000,
        )

    def _report_database_recovery(self):
        report = self.db.recovery or {}
        QMessageBox.information(
            self,
            "База восстановлена",
            "Файл данных приложения был повреждён (так бывает после внезапного "
            "выключения компьютера) и восстановлен автоматически.\n\n"
            f"Сохранено записей: {report.get('restored', 0)}, "
            f"отброшено повреждённых: {report.get('skipped', 0)}.\n\n"
            "Логин, пароль и почта хранятся отдельно и не пострадали. Проверьте "
            "группу и имя в «Настройках».\n\n"
            f"Копия повреждённого файла: {report.get('backup', '')}",
        )

    def _build_ui(self):
        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        sidebar = QWidget(objectName="sidebar")
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(0, 0, 0, 16)
        brand = QLabel("MIREA\nLecture Assistant", objectName="brand")
        brand.setWordWrap(True)
        side.addWidget(brand)
        self.nav_buttons = []
        for index, text in enumerate(("Расписание", "QR-сканер", "История", "Настройки", "MCP")):
            button = QPushButton(text, objectName="nav")
            button.setToolTip(f"Ctrl+{index + 1}")
            button.setCheckable(True)
            button.clicked.connect(partial(self._show_page, index))
            side.addWidget(button)
            self.nav_buttons.append(button)
        side.addStretch()
        # Shown only when a newer version is out: one click installs it.
        self.update_button = QPushButton("")
        self.update_button.setToolTip("Скачать и установить новую версию; данные и вход сохранятся")
        self.update_button.clicked.connect(self._start_update)
        self.update_button.hide()
        update_row = QHBoxLayout()
        update_row.setContentsMargins(12, 0, 12, 8)
        update_row.addWidget(self.update_button)
        side.addLayout(update_row)
        self.auth_status = QLabel()
        self.auth_status.setWordWrap(True)
        side.addWidget(self.auth_status)
        self.retry_login_button = QPushButton("Повторить вход", objectName="secondary")
        self.retry_login_button.setToolTip(
            "Войти сейчас с сохранёнными логином и паролем; код приложение возьмёт из почты само"
        )
        self.retry_login_button.clicked.connect(self._retry_login_now)
        retry_row = QHBoxLayout()
        retry_row.setContentsMargins(12, 0, 12, 8)
        retry_row.addWidget(self.retry_login_button)
        side.addLayout(retry_row)
        self._set_auth_state("signed_out")
        root.addWidget(sidebar)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._schedule_page())
        self.pages.addWidget(self._scanner_page())
        self.pages.addWidget(self._history_page())
        self.pages.addWidget(self._settings_page())
        self.pages.addWidget(self._mcp_page())
        root.addWidget(self.pages, 1)
        self.setCentralWidget(central)
        self.wheel_guard = WheelGuard(self)
        self.wheel_guard.guard(central)
        self._show_page(0)
        self.activity_label = QLabel("", objectName="activity")
        self.statusBar().addPermanentWidget(self.activity_label)
        for index in range(len(self.nav_buttons)):
            QShortcut(QKeySequence(f"Ctrl+{index + 1}"), self, partial(self._show_page, index))
        QShortcut(QKeySequence("F5"), self, self.refresh_schedule)

    def _page_shell(self, title: str, subtitle: str):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(30, 26, 30, 26)
        heading = QLabel(title, objectName="pageTitle")
        hint = QLabel(subtitle, objectName="muted")
        hint.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(hint)
        layout.addSpacing(12)
        return page, layout

    def _mcp_page(self):
        page, layout = self._page_shell(
            "MCP", "Отдельное подключение вашего ИИ-клиента. Собственных моделей и чата здесь нет."
        )
        self.mcp_status = QLabel()
        self.mcp_status.setWordWrap(True)
        self.mcp_status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.mcp_status)
        self.mcp_install_button = QPushButton("Скачать и установить MCP")
        self.mcp_install_button.clicked.connect(self._install_mcp)
        layout.addWidget(self.mcp_install_button)
        self.mcp_enabled = QCheckBox("Разрешить локальное MCP-подключение")
        self.mcp_enabled.setChecked(bool(self.db.get_setting("mcp_enabled", False)))
        self.mcp_enabled.toggled.connect(self._mcp_access_changed)
        layout.addWidget(self.mcp_enabled)
        self.mcp_allow_changes = QCheckBox("Разрешить ИИ менять настройки и правила предметов")
        self.mcp_allow_changes.setChecked(bool(self.db.get_setting("mcp_allow_changes", False)))
        self.mcp_allow_changes.toggled.connect(
            lambda enabled: self.db.set_setting("mcp_allow_changes", enabled)
        )
        layout.addWidget(self.mcp_allow_changes)
        self.mcp_config_button = QPushButton("Скопировать конфигурацию подключения")
        self.mcp_config_button.clicked.connect(self._copy_mcp_config)
        layout.addWidget(self.mcp_config_button)
        hint = QLabel(
            "Установите MCP, разрешите подключение и добавьте конфигурацию в ИИ-клиент "
            "с поддержкой локального MCP (stdio). Клиент сам запускает MCP.\n\n"
            "Без разрешения изменений доступны только диагностика и чтение настроек. "
            "Пароли, коды входа, QR и сообщения лекций недоступны. При обновлении MCP "
            "переподключите его в ИИ-клиенте; Lecture Assistant обновлять не нужно."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        repository = QPushButton("Репозиторий и релизы MCP", objectName="secondary")
        repository.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(f"https://github.com/{mcp_install.REPOSITORY}"))
        )
        layout.addWidget(repository)
        layout.addStretch()
        return page

    def _mcp_root(self):
        return self.mcp_access.root / "mcp"

    def _refresh_mcp_status(self):
        if self.mcp_leftovers and not self.mcp_busy:
            # Never during an install: its staging folder is not a leftover yet.
            self.mcp_leftovers = not mcp_install.clean_leftovers(self._mcp_root())
        release = mcp_install.installed(self._mcp_root())
        installed = release is not None
        self.mcp_enabled.setEnabled(installed)
        self.mcp_allow_changes.setEnabled(installed and self.mcp_enabled.isChecked())
        self.mcp_config_button.setEnabled(installed)
        self.mcp_install_button.setEnabled(not self.mcp_busy)
        self.mcp_install_button.setText(
            "Устанавливаем…"
            if self.mcp_busy
            else "Проверить обновление MCP"
            if installed
            else "Скачать и установить MCP"
        )
        lines = [f"Установлен MCP {release['version']}" if installed else "MCP не установлен"]
        if not self.mcp_access.server:
            lines.append("Подключение к приложению выключено")
        else:
            clients = self.mcp_access.connected_clients()
            if not clients:
                lines.append("Ожидает подключения ИИ-клиента")
            for client in clients:
                state = (
                    "подключён" if client["state"] == "connected" else "ожидает первого запроса ИИ"
                )
                lines.append(f"{client['name']} · MCP {client['version']} · {state}")
        self.mcp_status.setText("\n".join(lines))

    def _mcp_access_changed(self, enabled):
        self.db.set_setting("mcp_enabled", enabled)
        self._sync_mcp_access()

    def _sync_mcp_access(self):
        try:
            if self.mcp_enabled.isChecked() and mcp_install.installed(self._mcp_root()):
                self.mcp_access.start()
            else:
                self.mcp_access.stop()
        except OSError:
            log.warning("mcp_access_unavailable")
            self.statusBar().showMessage("Не удалось включить MCP-подключение", 5000)
        self._refresh_mcp_status()

    def _install_mcp(self):
        if self.mcp_busy:
            return
        self.mcp_busy = True
        self._refresh_mcp_status()

        def install():
            release = mcp_install.latest()
            current = mcp_install.installed(self._mcp_root())
            if current and not updater.is_newer(release["version"], current["version"]):
                return current
            return mcp_install.install(self._mcp_root(), release)

        def done(release):
            self.mcp_busy = False
            self.mcp_leftovers = True
            self._sync_mcp_access()
            self.statusBar().showMessage(f"MCP {release['version']} установлен", 5000)

        def failed(_message):
            self.mcp_busy = False
            self._refresh_mcp_status()
            QMessageBox.warning(
                self,
                "MCP",
                "Не удалось установить MCP. "
                "Проверьте интернет и доступность релиза; текущая версия сохранена.",
            )

        self._run(install, done, "Скачиваем отдельный MCP…", failed=failed)

    def _copy_mcp_config(self):
        import json

        config = mcp_install.client_config(self._mcp_root(), self.mcp_access.root)
        QGuiApplication.clipboard().setText(json.dumps(config, ensure_ascii=False, indent=2))
        self.statusBar().showMessage("Конфигурация MCP скопирована; добавьте её в ИИ-клиент", 5000)

    def _mcp_rpc(self, job):
        if job.cancelled:
            return
        try:
            if not self.mcp_enabled.isChecked() or not self.mcp_access.server:
                raise ValueError("MCP access is disabled")
            method, params = job.method, job.params
            write = method in {"update_settings", "set_subject_rule"}
            if write and not self.mcp_allow_changes.isChecked():
                raise ValueError("Changes are disabled in the MCP tab")
            if write and self._settings_dirty():
                raise ValueError("Save or discard unsaved settings before using MCP")
            if method == "status":
                result = {
                    "app_version": __version__,
                    "protocol": 1,
                    "session_present": bool(self.mirea.session),
                    "scanner_running": self.scan_timer.isActive(),
                    "last_capture_age_seconds": round(time.monotonic() - self.last_capture_at, 1)
                    if self.last_capture_at
                    else None,
                    "active_lesson_id": self.active_lecture_id,
                    "changes_allowed": self.mcp_allow_changes.isChecked(),
                }
            elif method == "get_settings":
                result = {
                    key: self.db.get_setting(key, default)
                    for key, (_, default, _) in MCP_SETTINGS.items()
                }
                result["schema"] = {
                    key: {"type": kind.__name__, "default": default, "range": bounds}
                    for key, (kind, default, bounds) in MCP_SETTINGS.items()
                }
            elif method == "update_settings":
                values = validate_settings(params.get("settings"))
                # One transaction, so a rejected/partial update cannot leak through.
                import json

                with self.db.connection() as conn:
                    for key, value in values.items():
                        conn.execute(
                            "INSERT INTO settings(key,value) VALUES(?,?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, json.dumps(value, ensure_ascii=False)),
                        )
                self._load_settings()
                self.scan_timer.setInterval(self.scan_interval.value() * 1000)
                if {"copy_manual_codes", "type_manual_codes"} & values.keys():
                    self._restart_code_watcher()
                self._update_now_card()
                result = {"updated": sorted(values)}
                log.info("mcp_settings_updated keys=%s", ",".join(sorted(values)))
            elif method == "get_schedule":
                result = {
                    "lessons": [
                        {
                            "id": lesson.external_id,
                            "subject": lesson.subject_name,
                            "type": lesson.lesson_type,
                            "start": lesson.start_at.isoformat(),
                            "end": lesson.end_at.isoformat(),
                        }
                        for lesson in self.db.list_lessons()[:200]
                    ]
                }
            elif method in {"get_subject_rules", "set_subject_rule"}:
                subjects = sorted({lesson.subject_name for lesson in self.db.list_lessons()})
                if method == "set_subject_rule":
                    subject, mode = params.get("subject"), params.get("mode")
                    if subject not in subjects or mode not in {"AUTO", "ASK", "IGNORE"}:
                        raise ValueError("Choose an existing subject and AUTO/ASK/IGNORE")
                    self.db.set_rule(subject, RuleMode(mode))
                    self._fill_schedule()
                    self._update_now_card()
                    log.info("mcp_subject_rule_updated")
                result = {
                    "rules": {subject: self.db.get_rule(subject).value for subject in subjects}
                }
            else:
                raise ValueError("Unsupported MCP method")
            job.result = {"result": result}
        except ValueError as exc:
            job.result = {"error": str(exc)}
        except Exception:  # noqa: BLE001 - never send exception contents/secrets to the client
            log.warning("mcp_request_failed")
            job.result = {"error": "Application could not complete the request"}
        finally:
            job.done.set()

    def _schedule_page(self):
        page, layout = self._page_shell(
            "Расписание", "Ближайшие занятия и постоянные правила для предметов"
        )
        actions = QHBoxLayout()
        self.group_label = QLabel("Группа не указана")
        self.group_label.setStyleSheet("font-weight: 600")
        actions.addWidget(self.group_label)
        actions.addWidget(QLabel("Показывать:"))
        self.lesson_type_filter = QComboBox()
        self.lesson_type_filter.addItem("Все типы", "")
        self.lesson_type_filter.setToolTip("Фильтр влияет только на отображение расписания")
        self.lesson_type_filter.currentIndexChanged.connect(self._schedule_filter_changed)
        actions.addWidget(self.lesson_type_filter)
        actions.addStretch()
        self.login_button = QPushButton("Войти в MIREA")
        self.login_button.clicked.connect(self.login)
        actions.addWidget(self.login_button)
        refresh = QPushButton("Обновить", objectName="secondary")
        refresh.setToolTip("F5")
        refresh.clicked.connect(self.refresh_schedule)
        actions.addWidget(refresh)
        layout.addLayout(actions)
        self.now_card = QLabel(objectName="nowCard")
        self.now_card.setWordWrap(True)
        self.now_card.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.now_card)
        rules = QHBoxLayout()
        rules.addWidget(QLabel("Автооткрытие предмета:"))
        self.subject_rule_subject = QComboBox()
        self.subject_rule_subject.setMinimumWidth(330)
        self.subject_rule_subject.currentTextChanged.connect(self._selected_subject_changed)
        rules.addWidget(self.subject_rule_subject, 1)
        self.subject_rule_mode = QComboBox()
        for mode in RuleMode:
            self.subject_rule_mode.addItem(MODE_LABELS[mode], mode.value)
        self.subject_rule_mode.setToolTip(
            "Авто — открыть лекцию самому; Спрашивать — предложить; Не открывать — пропускать"
        )
        self.subject_rule_mode.currentIndexChanged.connect(self._selected_rule_changed)
        rules.addWidget(self.subject_rule_mode)
        layout.addLayout(rules)
        self.schedule_table = QTableWidget(0, 9)
        self.schedule_table.setHorizontalHeaderLabels(
            (
                "Дата",
                "Время",
                "Предмет",
                "Тип",
                "Преподаватель",
                "Режим",
                "Ссылка",
                "",
                "",
            )
        )
        self.schedule_table.verticalHeader().setVisible(False)
        self.schedule_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        header = self.schedule_table.horizontalHeader()
        for column in (0, 1, 3, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        # The teacher moves into the subject's tooltip: nine columns did not fit
        # and every header ended up clipped.
        self.schedule_table.setColumnHidden(4, True)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(8, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.schedule_table, 1)
        return page

    def _scanner_page(self):
        page, layout = self._page_shell(
            "QR-сканер",
            "Сканирует вкладку или мониторы и автоматически отправляет валидный QR посещаемости.",
        )
        card = QGroupBox(objectName="card")
        box = QVBoxLayout(card)
        self.last_scan_label = QLabel("QR ещё не находили")
        self.last_scan_label.setStyleSheet("font-size: 22px; font-weight: 650;")
        box.addWidget(self.last_scan_label)
        self.last_scan_detail = QLabel("Последнее сканирование появится здесь", objectName="muted")
        box.addWidget(self.last_scan_detail)
        row = QHBoxLayout()
        self.scan_button = QPushButton("Начать сканирование экрана")
        self.scan_button.clicked.connect(self.toggle_scanner)
        row.addWidget(self.scan_button)
        self.scan_state = QLabel("● Остановлен")
        self.scan_state.setStyleSheet(f"color: {self.colors['muted']}")
        row.addWidget(self.scan_state)
        row.addStretch()
        box.addLayout(row)
        layout.addWidget(card)

        manual = QGroupBox("Обработать QR вручную")
        form = QHBoxLayout(manual)
        self.manual_qr = QLineEdit()
        self.manual_qr.setPlaceholderText("Вставьте ссылку из QR-кода")
        self.manual_qr.returnPressed.connect(self._manual_scan)
        form.addWidget(self.manual_qr, 1)
        check = QPushButton("Проверить")
        check.clicked.connect(self._manual_scan)
        form.addWidget(check)
        layout.addWidget(manual)
        layout.addStretch()
        return page

    def _history_page(self):
        page, layout = self._page_shell(
            "История QR", "Содержимое токенов не хранится — только безопасный отпечаток"
        )
        self.history_table = QTableWidget(0, 4)
        self.history_table.setHorizontalHeaderLabels(("Когда", "Статус", "Предмет", "Результат"))
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        history_header = self.history_table.horizontalHeader()
        history_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        history_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        history_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        history_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.history_table, 1)
        return page

    def _settings_page(self):
        page, layout = self._page_shell(
            "Настройки", "Параметры сохраняются локально на этом компьютере"
        )
        content = QWidget()
        sections = QVBoxLayout(content)
        sections.setContentsMargins(0, 0, 12, 0)

        study = self._settings_section(sections, "Учёба")
        self.group_edit = QLineEdit()
        self.group_edit.setPlaceholderText("Например, ИКБО-01-24")
        study.addRow("Группа", self.group_edit)
        self.student_name = QLineEdit()
        self.student_name.setPlaceholderText("Иванов Иван")
        self.student_name.setToolTip("Нужно только для резервного сообщения в чат лекции")
        study.addRow("Фамилия и имя", self.student_name)
        self.join_before = QSpinBox()
        self.join_before.setRange(0, 30)
        self.join_before.setSuffix(" мин")
        study.addRow("Подготовиться до пары", self.join_before)

        lecture = self._settings_section(sections, "Лекция и сканирование")
        self.scan_interval = QSpinBox()
        self.scan_interval.setRange(1, 5)
        self.scan_interval.setSuffix(" сек")
        lecture.addRow("Интервал сканирования", self.scan_interval)
        self.direct_capture = QCheckBox("Сканировать вкладку браузера напрямую")
        lecture.addRow("Источник изображения", self.direct_capture)
        self.hd_capture = QCheckBox("Захватывать кадр 1920 × 1080 независимо от размера окна")
        self.hd_capture.setToolTip(
            "Без этого QR в маленьком окне слишком мелкий, чтобы его распознать"
        )
        lecture.addRow("Качество захвата", self.hd_capture)
        self.compact_window = QCheckBox("Открывать лекцию в компактном окне 520 × 360")
        lecture.addRow("Размер окна", self.compact_window)
        self.mute_lecture = QCheckBox("Открывать лекцию без звука")
        lecture.addRow("Звук", self.mute_lecture)
        self.minimize_on_open = QCheckBox("Сворачивать окно лекции сразу после открытия")
        self.minimize_on_open.setToolTip(
            "Захват идёт с самой вкладки, поэтому свёрнутое окно сканируется так же"
        )
        lecture.addRow("После открытия", self.minimize_on_open)
        self.minimize_after_qr = QCheckBox("Сворачивать окно лекции после распознавания QR")
        lecture.addRow("После QR", self.minimize_after_qr)
        self.close_tab_after = QCheckBox("Закрывать вкладку лекции, когда пара закончилась")
        lecture.addRow("После пары", self.close_tab_after)
        self.chat_fallback = QCheckBox("Использовать резервное сообщение в чат")
        self.chat_fallback.setToolTip(
            "Если отметка не проходит, в чат уходит «Фамилия Имя группа» — не чаще раза за пару"
        )
        lecture.addRow("Резервный сценарий", self.chat_fallback)

        sign_in = self._settings_section(sections, "Вход и почта для кода")
        self.auto_login = QCheckBox("Автоматически входить и получать код из почты")
        sign_in.addRow("Вход", self.auto_login)
        self.email_provider = QComboBox()
        _fill_email_providers(self.email_provider)
        sign_in.addRow("Провайдер почты", self.email_provider)
        self.email_address = QLineEdit()
        self.email_address.setPlaceholderText("example@mail.ru")
        sign_in.addRow("Почта для кода", self.email_address)
        self.email_app_password = QLineEdit()
        self.email_app_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.email_app_password.setPlaceholderText("Пароль приложения почты")
        sign_in.addRow("Пароль приложения", self.email_app_password)
        self.imap_username = QLineEdit()
        self.imap_username.setPlaceholderText("Обычно определяется по адресу")
        sign_in.addRow("Логин IMAP", self.imap_username)
        self.imap_host = QLineEdit()
        self.imap_port = QSpinBox()
        self.imap_port.setRange(1, 65535)
        self.imap_port.setValue(993)
        sign_in.addRow("IMAP-сервер", self.imap_host)
        sign_in.addRow("Порт IMAP", self.imap_port)
        self.email_hint = QLabel()
        self.email_hint.setOpenExternalLinks(True)
        self.email_hint.setWordWrap(True)
        self.email_hint.setObjectName("muted")
        sign_in.addRow("", self.email_hint)
        self.email_provider.currentIndexChanged.connect(
            lambda: _update_imap_fields(
                self.email_provider, self.imap_host, self.imap_port, self.email_hint
            )
        )
        _update_imap_fields(self.email_provider, self.imap_host, self.imap_port, self.email_hint)
        test_email = QPushButton("Проверить подключение к почте", objectName="secondary")
        test_email.clicked.connect(self._test_email_connection)
        sign_in.addRow("", test_email)
        self.copy_manual_codes = QCheckBox("Когда вхожу сам, копировать код из письма МИРЭА")
        self.copy_manual_codes.setToolTip(
            "Приложение ждёт письмо, не нагружая компьютер: почта сама сообщает о новом "
            "письме. Коды для входов самого приложения не копируются."
        )
        sign_in.addRow("Мой вход", self.copy_manual_codes)
        self.type_manual_codes = QCheckBox("и сразу вводить его на странице МИРЭА в браузере")
        self.type_manual_codes.setToolTip(
            "Через расширение, даже в неактивной вкладке. Без расширения код "
            "только в буфере обмена — вставьте его Ctrl+V"
        )
        if sys.platform != "win32":
            self.type_manual_codes.setEnabled(False)
        self.copy_manual_codes.toggled.connect(self.type_manual_codes.setEnabled)
        sign_in.addRow("", self.type_manual_codes)
        max_extension = QPushButton(
            "Расширение «пропуск МАКС» для браузера…", objectName="secondary"
        )
        max_extension.setToolTip(
            "Само нажимает «Пропустить» на странице «Подтверждение через МАКС», "
            "когда вы входите на сайт МИРЭА в своём браузере"
        )
        max_extension.clicked.connect(self._install_browser_extension)
        sign_in.addRow("МАКС", max_extension)

        appearance = self._settings_section(sections, "Внешний вид")
        self.theme_choice = QComboBox()
        for value, label in THEME_CHOICES:
            self.theme_choice.addItem(label, value)
        self.theme_choice.currentIndexChanged.connect(self._theme_choice_changed)
        appearance.addRow("Оформление", self.theme_choice)

        startup = self._settings_section(sections, "Запуск")
        self.autostart_check = QCheckBox("Запускать вместе с Windows (сразу в трей)")
        self.autostart_check.setEnabled(autostart.available())
        if not autostart.available():
            self.autostart_check.setToolTip("Работает в собранной программе для Windows")
        self.autostart_check.setChecked(bool(self.db.get_setting("autostart", True)))
        self.autostart_check.toggled.connect(self._autostart_toggled)
        startup.addRow("", self.autostart_check)

        updates = self._settings_section(sections, "Обновления")
        updates.addRow("Установлена версия", QLabel(__version__))
        self.auto_update_check = QCheckBox("Проверять обновления автоматически")
        self.auto_update_check.setChecked(bool(self.db.get_setting("check_updates", True)))
        self.auto_update_check.toggled.connect(
            lambda checked: self.db.set_setting("check_updates", bool(checked))
        )
        updates.addRow("", self.auto_update_check)
        check_now = QPushButton("Проверить сейчас", objectName="secondary")
        check_now.clicked.connect(lambda: self._check_for_updates(manual=True))
        updates.addRow("", check_now)

        diagnostics = self._settings_section(sections, "Диагностика")
        open_logs = QPushButton("Открыть папку журналов", objectName="secondary")
        open_logs.clicked.connect(self._open_logs_folder)
        diagnostics.addRow("Журналы", open_logs)
        sections.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        footer = QHBoxLayout()
        self.settings_dirty_label = QLabel("", objectName="dirty")
        footer.addWidget(self.settings_dirty_label)
        footer.addStretch()
        self.save_settings_button = QPushButton("Сохранить")
        self.save_settings_button.setShortcut(QKeySequence.StandardKey.Save)
        self.save_settings_button.setToolTip("Ctrl+S")
        self.save_settings_button.clicked.connect(self._save_settings)
        footer.addWidget(self.save_settings_button)
        layout.addLayout(footer)

        for line_edit in (
            self.group_edit,
            self.student_name,
            self.email_address,
            self.email_app_password,
            self.imap_username,
            self.imap_host,
        ):
            line_edit.textEdited.connect(self._settings_changed)
        for spin in (self.join_before, self.scan_interval, self.imap_port):
            spin.valueChanged.connect(self._settings_changed)
        for check in (
            self.direct_capture,
            self.hd_capture,
            self.compact_window,
            self.mute_lecture,
            self.minimize_on_open,
            self.minimize_after_qr,
            self.close_tab_after,
            self.chat_fallback,
            self.auto_login,
            self.copy_manual_codes,
            self.type_manual_codes,
        ):
            check.toggled.connect(self._settings_changed)
        self.email_provider.currentIndexChanged.connect(self._settings_changed)
        return page

    @staticmethod
    def _settings_section(parent_layout: QVBoxLayout, title: str) -> QFormLayout:
        box = QGroupBox(title, objectName="section")
        form = QFormLayout(box)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setVerticalSpacing(10)
        parent_layout.addWidget(box)
        return form

    def _theme_choice_changed(self, _index: int):
        # Cosmetic, so it is applied and kept at once rather than on «Сохранить».
        self.db.set_setting("theme", self.theme_choice.currentData() or "system")
        self._apply_theme()

    def _theme_is_dark(self) -> bool:
        choice = self.db.get_setting("theme", "system")
        if choice in ("light", "dark"):
            return choice == "dark"
        hints = QGuiApplication.styleHints()
        scheme = getattr(hints, "colorScheme", None)
        if scheme is None:
            return False
        return scheme() == Qt.ColorScheme.Dark

    def _color_scheme_changed(self, _scheme):
        self._apply_theme()

    def _apply_theme(self):
        name = "dark" if self._theme_is_dark() else "light"
        self.colors = THEMES[name]
        self.setStyleSheet(STYLE.substitute(self.colors))
        # Dialogs, message boxes and menus outside the stylesheet follow the palette.
        palette = QPalette()
        roles = {
            QPalette.ColorRole.Window: "bg",
            QPalette.ColorRole.WindowText: "text",
            QPalette.ColorRole.Base: "surface",
            QPalette.ColorRole.AlternateBase: "header",
            QPalette.ColorRole.Text: "text",
            QPalette.ColorRole.Button: "surface",
            QPalette.ColorRole.ButtonText: "text",
            QPalette.ColorRole.ToolTipBase: "surface",
            QPalette.ColorRole.ToolTipText: "text",
            QPalette.ColorRole.Highlight: "accent",
            QPalette.ColorRole.HighlightedText: "on_accent",
            QPalette.ColorRole.PlaceholderText: "muted",
            QPalette.ColorRole.Link: "info",
        }
        for role, token in roles.items():
            palette.setColor(role, QColor(self.colors[token]))
        app = QApplication.instance()
        if app is not None:
            app.setPalette(palette)
        self.scan_state.setStyleSheet(
            f"color: {self.colors['ok' if self.scan_timer.isActive() else 'muted']}"
            if hasattr(self, "scan_timer")
            else f"color: {self.colors['muted']}"
        )
        self.schedule_signature = None
        self._fill_schedule()
        self._fill_history()
        log.info("theme_applied theme=%s", name)

    def _settings_changed(self, *_args):
        self.settings_dirty_label.setText("● Есть несохранённые изменения")

    def _settings_dirty(self) -> bool:
        return bool(self.settings_dirty_label.text())

    def _build_tray(self):
        icon = QApplication.windowIcon()
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("MIREA Lecture Assistant")
        menu = QMenu(self)
        self.tray_status_action = QAction("Ближайших пар нет", self)
        self.tray_status_action.setEnabled(False)
        menu.addAction(self.tray_status_action)
        menu.addSeparator()
        show_action = QAction("Открыть", self)
        show_action.triggered.connect(self._restore)
        menu.addAction(show_action)
        self.pause_action = QAction("Начать QR-сканирование", self)
        self.pause_action.triggered.connect(self.toggle_scanner)
        menu.addAction(self.pause_action)
        menu.addSeparator()
        exit_action = QAction("Выход", self)
        exit_action.triggered.connect(self._quit)
        menu.addAction(exit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: (
                self._restore()
                if reason
                in (
                    QSystemTrayIcon.ActivationReason.Trigger,
                    QSystemTrayIcon.ActivationReason.DoubleClick,
                )
                else None
            )
        )
        self.tray.show()

    def _show_page(self, index: int):
        leaving_settings = self.pages.currentIndex() == SETTINGS_PAGE and index != SETTINGS_PAGE
        if leaving_settings and self._settings_dirty():
            # Unsaved edits are dropped, not left half-applied: several of them
            # (minutes before a pair, the scan interval) are read from the fields.
            self._load_settings()
            log.info("settings_unsaved_changes_discarded")
            self.statusBar().showMessage("Несохранённые изменения настроек отменены", 4000)
        self.pages.setCurrentIndex(index)
        for i, button in enumerate(self.nav_buttons):
            button.setChecked(i == index)

    def _load_settings(self):
        self.group_edit.setText(self.db.get_setting("group", ""))
        self.join_before.setValue(int(self.db.get_setting("join_before", 5)))
        self.scan_interval.setValue(int(self.db.get_setting("scan_interval", 2)))
        self.direct_capture.setChecked(bool(self.db.get_setting("direct_capture", True)))
        self.compact_window.setChecked(bool(self.db.get_setting("compact_window", True)))
        self.hd_capture.setChecked(bool(self.db.get_setting("hd_capture", True)))
        self._apply_capture_quality()
        self.mute_lecture.setChecked(bool(self.db.get_setting("mute_lecture", True)))
        self.close_tab_after.setChecked(bool(self.db.get_setting("close_tab_after", True)))
        self.minimize_on_open.setChecked(bool(self.db.get_setting("minimize_on_open", True)))
        self.minimize_after_qr.setChecked(bool(self.db.get_setting("minimize_after_qr", True)))
        self.chat_fallback.setChecked(bool(self.db.get_setting("chat_fallback", True)))
        self.student_name.setText(self.db.get_setting("student_name", ""))
        self.auto_login.setChecked(bool(self.db.get_setting("auto_login", True)))
        self.copy_manual_codes.setChecked(bool(self.db.get_setting("copy_manual_codes", True)))
        self.type_manual_codes.setChecked(bool(self.db.get_setting("type_manual_codes", True)))
        self.type_manual_codes.setEnabled(
            sys.platform == "win32" and self.copy_manual_codes.isChecked()
        )
        self.theme_choice.blockSignals(True)
        index = self.theme_choice.findData(self.db.get_setting("theme", "system"))
        self.theme_choice.setCurrentIndex(max(index, 0))
        self.theme_choice.blockSignals(False)
        try:
            email_credentials = self.session_store.load_email_credentials()
        except Exception:  # noqa: BLE001
            email_credentials = None
        if email_credentials:
            self.email_address.setText(email_credentials.address)
            index = self.email_provider.findData(email_credentials.provider)
            self.email_provider.setCurrentIndex(max(index, 0))
            self.imap_host.setText(email_credentials.imap_host)
            self.imap_port.setValue(email_credentials.imap_port)
            self.imap_username.setText(email_credentials.imap_username)
            self.email_app_password.setPlaceholderText("Сохранён в защищённом хранилище")
        else:
            self.email_address.clear()
            self.imap_username.clear()
            self.email_provider.setCurrentIndex(max(self.email_provider.findData("auto"), 0))
            _update_imap_fields(
                self.email_provider, self.imap_host, self.imap_port, self.email_hint
            )
            self.email_app_password.setPlaceholderText("Пароль приложения почты")
        # A password typed and never saved is not kept in the field.
        self.email_app_password.clear()
        self._update_group_label()
        self.settings_dirty_label.setText("")

    def _save_settings(self):
        # The mail account is validated first: an error there used to leave the
        # other settings saved while the page reported that nothing was.
        if not self._save_email_settings():
            return
        self.db.set_setting("group", self.group_edit.text().strip())
        self.db.set_setting("join_before", self.join_before.value())
        self.db.set_setting("scan_interval", self.scan_interval.value())
        self.db.set_setting("direct_capture", self.direct_capture.isChecked())
        self.db.set_setting("compact_window", self.compact_window.isChecked())
        self.db.set_setting("hd_capture", self.hd_capture.isChecked())
        self._apply_capture_quality()
        self.db.set_setting("mute_lecture", self.mute_lecture.isChecked())
        self.db.set_setting("close_tab_after", self.close_tab_after.isChecked())
        self.db.set_setting("minimize_on_open", self.minimize_on_open.isChecked())
        self.db.set_setting("minimize_after_qr", self.minimize_after_qr.isChecked())
        self.db.set_setting("chat_fallback", self.chat_fallback.isChecked())
        self.db.set_setting("student_name", self.student_name.text().strip())
        self.db.set_setting("auto_login", self.auto_login.isChecked())
        self.db.set_setting("copy_manual_codes", self.copy_manual_codes.isChecked())
        self.db.set_setting("type_manual_codes", self.type_manual_codes.isChecked())
        self._restart_code_watcher()
        if self.student_name.text().strip() and self.group_edit.text().strip():
            self.chat_config_warned = False
        self.scan_timer.setInterval(self.scan_interval.value() * 1000)
        self._update_group_label()
        self.settings_dirty_label.setText("")
        self._update_now_card()
        self.statusBar().showMessage("Настройки сохранены", 3000)
        log.info(
            "settings_saved scan_interval=%s direct_capture=%s compact=%s hd_capture=%s "
            "muted=%s minimize_on_open=%s minimize_after_qr=%s chat_fallback=%s auto_login=%s",
            self.scan_interval.value(),
            self.direct_capture.isChecked(),
            self.compact_window.isChecked(),
            self.hd_capture.isChecked(),
            self.mute_lecture.isChecked(),
            self.minimize_on_open.isChecked(),
            self.minimize_after_qr.isChecked(),
            self.chat_fallback.isChecked(),
            self.auto_login.isChecked(),
        )

    def _save_email_settings(self) -> bool:
        email_address = self.email_address.text().strip()
        email_password = self.email_app_password.text()
        try:
            existing_email = self.session_store.load_email_credentials()
            if not email_address:
                self.session_store.clear_email_credentials()
            else:
                if not email_password and (
                    not existing_email
                    or existing_email.address.casefold() != email_address.casefold()
                ):
                    self.statusBar().showMessage(
                        "Для нового почтового адреса укажите пароль приложения",
                        6000,
                    )
                    return False
                account = _email_account_from_fields(
                    email_address,
                    email_password or (existing_email.password if existing_email else ""),
                    self.email_provider.currentData() or "auto",
                    self.imap_host.text(),
                    self.imap_port.value(),
                    self.imap_username.text(),
                )
                self.session_store.save_email_credentials(account)
                self.email_app_password.clear()
                self.email_app_password.setPlaceholderText("Сохранён в защищённом хранилище")
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(
                f"Не удалось сохранить доступ к почте: {exc}",
                6000,
            )
            return False
        return True

    def _test_email_connection(self):
        address = self.email_address.text().strip()
        password = self.email_app_password.text()
        try:
            existing = self.session_store.load_email_credentials()
            if not password and existing and existing.address.casefold() == address.casefold():
                password = existing.password
            account = _email_account_from_fields(
                address,
                password,
                self.email_provider.currentData() or "auto",
                self.imap_host.text(),
                self.imap_port.value(),
                self.imap_username.text(),
            )
        except Exception as exc:  # noqa: BLE001 - validation and keyring backends vary
            self.statusBar().showMessage(f"Не удалось проверить почту: {exc}", 7000)
            return
        self._run(
            lambda: self.otp_reader.latest_uid(account),
            lambda _uid: self.statusBar().showMessage(
                f"Подключение к {account.provider_name} работает. Нажмите «Сохранить».", 7000
            ),
            "Проверяем подключение к почте…",
            failed=lambda message: QMessageBox.warning(
                self, "Не удалось подключиться к почте", message
            ),
        )

    def _apply_capture_quality(self):
        """A small lecture window must not shrink the frame the scanner sees."""
        self.browser.capture_size = (1920, 1080) if self.hd_capture.isChecked() else None

    def _install_browser_extension(self):
        """Put the МАКС-skipping extension in a folder of its own and say how to add it.

        A browser takes an unpacked extension only from a folder the person picks
        on its extensions page; the program can prepare the folder, not press that.
        """
        from .paths import data_dir, resource_path

        target = data_dir() / "browser-extension"
        try:
            self.code_bridge.prepare_extension(resource_path("browser_extension"), target)
        except OSError as exc:
            log.warning("browser_extension_copy_failed", exc_info=True)
            QMessageBox.warning(self, "Расширение", f"Не удалось подготовить папку: {exc}")
            return
        log.info("browser_extension_prepared")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
        QMessageBox.information(
            self,
            "Расширение МИРЭА: МАКС и код",
            "Папка с расширением открыта. Чтобы добавить его в браузер:\n\n"
            "1. Откройте страницу расширений: chrome://extensions "
            "(Edge — edge://extensions, Яндекс — browser://extensions).\n"
            "2. Включите «Режим разработчика».\n"
            "3. Нажмите «Загрузить распакованное расширение» и выберите папку:\n"
            f"{target}\n\n"
            "Папку не удаляйте: браузер берёт расширение из неё. Расширение работает "
            "только на sso.mirea.ru и нажимает «Пропустить», лишь когда страница сама "
            "это предлагает. Также вводит почтовый код в неактивной вкладке входа. "
            "Если расширение уже установлено, нажмите его кнопку обновления "
            "на странице расширений, чтобы загрузить новые разрешения.",
        )

    def _open_logs_folder(self):
        from .paths import data_dir

        log_dir = data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log.info("logs_folder_opened")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_dir)))

    def _update_group_label(self):
        group = self.db.get_setting("group", "")
        self.group_label.setText(f"Группа: {group}" if group else "Группа не указана")

    def _run(self, function, done, busy_text: str = "Выполняется…", failed=None, *, pool=None):
        operation = busy_text.casefold().replace("…", "").replace(" ", "_").replace(",", "")
        worker = Worker(function, operation)
        self._start_worker(
            worker, done, failed or self._operation_failed, busy_text=busy_text, pool=pool
        )

    def _start_worker(self, worker, done, failed, *, busy_text: str = "", pool=None):
        """Run a worker, keeping it alive until its result has been delivered.

        QThreadPool deletes a finished QRunnable, which takes its signal object
        with it; a queued result can then be dropped before the main thread ever
        sees it, leaving the operation hanging with no error anywhere.
        """
        worker.setAutoDelete(False)
        self.active_workers.add(worker)
        # What runs in the background is shown in its own corner of the status
        # bar. Clearing the message line on every completion used to wipe results
        # such as "Посещение отмечено" seconds after they appeared.
        if busy_text:
            self.busy_operations[id(worker)] = busy_text
            self._update_activity()

        def deliver(handler, value):
            self.active_workers.discard(worker)
            if self.busy_operations.pop(id(worker), None) is not None:
                self._update_activity()
            handler(value)

        worker.signals.done.connect(lambda value: deliver(done, value))
        worker.signals.failed.connect(lambda message: deliver(failed, message))
        (pool or self.pool).start(worker)

    def _update_activity(self):
        if not self.busy_operations:
            self.activity_label.setText("")
            return
        latest = list(self.busy_operations.values())[-1]
        extra = len(self.busy_operations) - 1
        self.activity_label.setText(f"⟳ {latest}" + (f" (+{extra})" if extra else ""))

    def _set_auth_state(self, state: str):
        text, colour = AUTH_STATES[state]
        self.auth_status.setText(text)
        self.auth_status.setStyleSheet(f"color: {colour}; padding: 12px 18px;")
        if hasattr(self, "login_button"):
            self.login_button.setText("Войти заново" if state == "signed_in" else "Войти в MIREA")

    def _background_problem(self, title: str, message: str):
        """Report a failure of unattended work without a modal dialog nobody will see."""
        log.warning("background_problem title=%s message=%s", title, message)
        self.statusBar().showMessage(f"{title}: {message.splitlines()[0]}", 15000)
        self.tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Warning, 10000)

    def _ask(self, title: str, text: str) -> bool:
        """A question the student has to answer: bring the window up first."""
        if self.isHidden() or self.isMinimized():
            self.tray.showMessage(title, text, QSystemTrayIcon.MessageIcon.Information, 8000)
            self._restore()
        answer = QMessageBox.question(self, title, text)
        return answer == QMessageBox.StandardButton.Yes

    def _operation_failed(self, message: str):
        log.error("ui_operation_failed message=%s", message)
        self.statusBar().clearMessage()
        QMessageBox.warning(self, "Не удалось выполнить действие", message or "Неизвестная ошибка")

    def login(self):
        if self.login_in_progress:
            # Changing credentials or flags under a running login corrupted it.
            self.statusBar().showMessage("Вход уже выполняется — дождитесь его окончания", 6000)
            return
        try:
            saved_email_credentials = self.session_store.load_email_credentials()
        except Exception:  # noqa: BLE001
            saved_email_credentials = None
        dialog = LoginDialog(self, saved_email_credentials)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        username = dialog.username.text().strip()
        password = dialog.password.text()
        if not username or not password:
            QMessageBox.warning(self, "Вход", "Введите логин и пароль")
            return
        self.pending_login_credentials = (username, password)
        log.info(
            "login_requested remember=%s email_configured=%s provider=%s",
            dialog.remember.isChecked(),
            bool(dialog.email_address.text().strip()),
            dialog.email_provider.currentData(),
        )
        email_address = dialog.email_address.text().strip()
        email_password = dialog.email_password.text()
        if not email_address:
            self.pending_email_credentials = None
        else:
            if not email_password and (
                not saved_email_credentials
                or saved_email_credentials.address.casefold() != email_address.casefold()
            ):
                QMessageBox.warning(
                    self, "Почта", "Для нового почтового адреса укажите пароль приложения"
                )
                return
            try:
                self.pending_email_credentials = _email_account_from_fields(
                    email_address,
                    email_password
                    or (saved_email_credentials.password if saved_email_credentials else ""),
                    dialog.email_provider.currentData() or "auto",
                    dialog.imap_host.text(),
                    dialog.imap_port.value(),
                    dialog.imap_username.text(),
                )
            except ValueError as exc:
                QMessageBox.warning(self, "Почта", str(exc))
                return
        self.remember_login_requested = dialog.remember.isChecked()
        self.automatic_login_cycle = False
        if self.remember_login_requested and self.pending_email_credentials:
            try:
                self.session_store.save_email_credentials(self.pending_email_credentials)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(self, "Почта", f"Не удалось сохранить доступ к почте: {exc}")
                return
        if self.remember_login_requested:
            try:
                self.session_store.save_credentials(username, password)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(
                    self,
                    "Вход",
                    f"Не удалось сохранить данные входа в защищённом хранилище: {exc}",
                )
                return
        self.login_started_at = datetime.now(UTC)
        self.otp_submission_attempted = False
        self.login_cycle_retries = 0
        self._run_initial_login(username, password, "Выполняется вход…")

    def _run_initial_login(self, username: str, password: str, busy_text: str):
        if self.login_in_progress:
            return
        self.login_in_progress = True
        self.awaiting_own_code = True
        self.pending_pulse_login = None
        self.session_recheck_scheduled = False
        if self.sdo_sign_in_lock.locked():
            # An СДО sign-in is waiting for its emailed code right now; starting
            # here would put two codes in one mailbox. Try again in a moment.
            self.login_in_progress = False
            log.info("login_deferred reason=sdo_sign_in_in_progress")
            self.statusBar().showMessage("Идёт вход в СДО — вход в MIREA через 30 секунд", 8000)
            QTimer.singleShot(
                30_000, lambda: self._run_initial_login(username, password, busy_text)
            )
            return
        # Codes are matched from this moment on. Set earlier, a login that waited
        # behind the СДО sign-in took that sign-in's code from the spam folder.
        self.login_started_at = datetime.now(UTC)
        self._set_auth_state("checking")

        def operation():
            latest_uid = None
            if self.pending_email_credentials:
                try:
                    latest_uid = self.otp_reader.latest_uid(self.pending_email_credentials)
                except Exception as exc:  # noqa: BLE001
                    log.warning("email_uid_snapshot_failed error=%s", exc)
            result = run_async(self.mirea.login(username, password))
            return result, latest_uid

        self._run(
            operation,
            self._initial_login_finished,
            busy_text,
            failed=self._initial_login_failed,
        )

    def _initial_login_failed(self, message: str):
        self.login_in_progress = False
        self._set_auth_state("signed_out")
        log.warning("automatic_login_failed message=%s", message)
        if self.automatic_login_cycle:
            self.automatic_login_cycle = False
            self._retry_automatic_login_later(message)
        else:
            self._operation_failed(message)

    def _retry_automatic_login_later(self, reason: str):
        """A few later attempts after an automatic login failed before any code.

        A server that did not answer is not a wrong password, and the stale
        session was already dropped: without this nothing signed in again that
        day. Codes stay safe, as this runs only when no code was requested.
        """
        attempt = self.login_retry_attempt
        if not should_retry_login(reason):
            self._schedule_login_retry(reason)
            self._background_problem(
                "Автовход не удался", f"{reason}\n\nВойдите вручную: «Войти в MIREA»."
            )
            return
        minutes = AUTO_LOGIN_RETRY_MINUTES[min(attempt, len(AUTO_LOGIN_RETRY_MINUTES) - 1)]
        self.login_retry_attempt += 1
        self.login_retry_scheduled = True
        self.login_retry_generation += 1
        generation = self.login_retry_generation
        log.info("automatic_login_retry_scheduled attempt=%s minutes=%s", attempt + 1, minutes)
        self.statusBar().showMessage(
            f"Автовход не удался: {reason.splitlines()[0] if reason else 'ошибка'} "
            f"Повторим через {minutes} мин.",
            15000,
        )
        QTimer.singleShot(
            minutes * 60_000,
            lambda: (
                self._run_scheduled_login() if generation == self.login_retry_generation else None
            ),
        )

    def _initial_login_finished(self, payload):
        self.login_in_progress = False
        result, self.email_uid_before_login = payload
        self._login_finished(result)

    def _login_finished(self, result, *, refresh_schedule: bool = True):
        self.login_in_progress = False
        if result.challenge:
            if self.otp_submission_attempted:
                if self.automatic_login_cycle:
                    log.warning("automatic_2fa_rejected; further login requires manual action")
                    self.automatic_login_cycle = False
                    self._set_auth_state("signed_out")
                    self._background_problem(
                        "Автовход не удался",
                        result.message or "Код не принят; войдите вручную",
                    )
                    if transient_login_failure(result.message):
                        self._retry_automatic_login_later(result.message)
                    return
                self._manual_2fa(
                    result.challenge,
                    result.message or "Автоматически полученный код не был принят.",
                )
                return
            log.info("login_2fa_required")
            email_credentials = self.pending_email_credentials
            if not email_credentials:
                try:
                    email_credentials = self.session_store.load_email_credentials()
                except Exception:  # noqa: BLE001
                    email_credentials = None
            # The flow stays "in progress" until the code is submitted: a second
            # login started meanwhile (schedule recovery, the watchdog) would
            # replace the SSO client and the code from this email would be sent
            # to the wrong flow. Slow providers (Яндекс, Mail.ru) hit that window.
            self.login_in_progress = True
            if not self._challenge_uses_email(result.challenge):
                # An authenticator app or MAX code never arrives by email; waiting
                # two minutes for it only delayed the prompt.
                self._manual_2fa(
                    result.challenge,
                    "MIREA просит код из приложения-аутентификатора или MAX, а не из почты.",
                )
            elif email_credentials:
                self._run(
                    lambda: self.otp_reader.wait_for_code(
                        email_credentials,
                        self.login_started_at,
                        after_uid=self.email_uid_before_login,
                    ),
                    lambda code: (
                        self._own_code_taken(code),
                        self._complete_2fa(result.challenge, code),
                    ),
                    "Ждём код из почты…",
                    lambda message: self._otp_wait_failed(result.challenge, message),
                )
            else:
                self._manual_2fa(
                    result.challenge,
                    "Укажите почту и пароль приложения в настройках для автоматического получения.",
                )
            return
        if not result.success:
            if getattr(result, "session_pending", False):
                self.pending_pulse_login = result
                self._persist_session()
                self._session_verified(SessionState.UNKNOWN)
                return
            # Never start another SSO flow after a rejected/failed code.
            # The account may be locked by repeated automatic challenges.
            self.pending_login_credentials = None
            self.pending_email_credentials = None
            self.login_in_progress = False
            self._set_auth_state("signed_out")
            diagnostic_id = secrets.token_hex(4)
            log.error("login_rejected id=%s message=%s", diagnostic_id, result.message)
            if self.automatic_login_cycle:
                # Nobody is at the screen for a modal dialog.
                self.automatic_login_cycle = False
                if self.otp_submission_attempted:
                    self._background_problem(
                        "Автовход не удался",
                        f"{result.message}\n\nКод диагностики: {diagnostic_id}",
                    )
                    if transient_login_failure(result.message):
                        self._retry_automatic_login_later(result.message)
                else:
                    self._retry_automatic_login_later(result.message or "")
                return
            self._operation_failed(f"{result.message}\n\nКод диагностики: {diagnostic_id}")
            return
        try:
            self.session_store.save(self.mirea.session)
            self.persisted_session = self._session_fingerprint(dict(self.mirea.session))
        except Exception as exc:  # noqa: BLE001 - OS keyring backends expose varied failures
            # The session works for this run; only the next start has to log in again.
            log.warning("session_persist_failed error=%s", exc)
            self.statusBar().showMessage(
                f"Вход выполнен, но сессия не сохранена — при следующем запуске потребуется вход: {exc}",
                8000,
            )
        if self.pending_login_credentials:
            try:
                if self.remember_login_requested:
                    self.session_store.save_credentials(*self.pending_login_credentials)
                    self.db.set_setting("auto_login", True)
                    self.auto_login.setChecked(True)
                else:
                    self.session_store.clear_credentials()
                    self.session_store.clear_email_credentials()
            except Exception as exc:  # noqa: BLE001
                self.statusBar().showMessage(
                    f"Вход выполнен, но данные входа не сохранены: {exc}", 6000
                )
        self.pending_login_credentials = None
        self.pending_email_credentials = None
        self.pending_pulse_login = None
        self.email_uid_before_login = None
        self.otp_submission_attempted = False
        self.login_cycle_retries = 0
        self.login_retry_attempt = 0
        self.login_retry_scheduled = False
        self.automatic_login_cycle = False
        self.session_recheck_scheduled = False
        self.session_obtained_at = time.monotonic()
        self._set_auth_state("signed_in")
        log.info("login_success")
        self.statusBar().showMessage("Вход выполнен", 3000)
        if refresh_schedule:
            self.refresh_schedule()
        for event_id in tuple(self.pending_qr):
            if event_id not in self.retry_scheduled:
                self._retry_attendance(event_id)

    @staticmethod
    def _challenge_uses_email(challenge) -> bool:
        """Only a recognised MAX or authenticator-app code skips the email wait.

        pymirea labels every form it recognises only by its HTML "otp", email
        ones included; narrowing the wait to "email_code" alone (0.2.3) sent such
        emailed codes to a manual prompt.
        """
        field = str(getattr(challenge, "field_name", "") or "").casefold()
        hidden = getattr(challenge, "hidden_fields", None) or {}
        kind = str(getattr(challenge, "kind", "") or "")
        if kind == "email_code" or field == "emailcode":
            return True
        max_messenger = field == "code" and str(hidden.get("login", "")).lower() == "true"
        authenticator_app = kind == "otp" and field == "otp" and not hidden
        return not (max_messenger or authenticator_app)

    def _complete_2fa(self, challenge, code: str):
        self.otp_submission_attempted = True
        self.login_in_progress = True
        self._run(
            lambda: run_async(self.mirea.complete_2fa(challenge, code)),
            self._login_finished,
            "Проверяем код…",
            lambda message: self._code_check_failed(challenge, message),
        )

    def _code_check_failed(self, challenge, message: str):
        """The code was there; sending it to MIREA failed."""
        if self.automatic_login_cycle:
            log.warning("automatic_code_check_failed message=%s", message)
            self.login_in_progress = False
            self.automatic_login_cycle = False
            self._set_auth_state("signed_out")
            self._background_problem("Код не удалось проверить", message)
            self._retry_automatic_login_later(message)
            return
        self._manual_2fa(challenge, message)

    def _manual_2fa(self, challenge, reason: str = ""):
        prompt = "Не удалось получить код автоматически. Введите код из письма:"
        if reason:
            prompt = f"{prompt}\n\n{reason}"
        # Timers keep firing while the dialog is open; no second login meanwhile.
        self.login_in_progress = True
        code, ok = QInputDialog.getText(self, "Двухфакторная авторизация", prompt)
        if ok and code.strip():
            self._complete_2fa(challenge, code.strip())
        else:
            self.login_in_progress = False
            self._set_auth_state("signed_out")

    def _otp_wait_failed(self, challenge, message: str):
        if self.automatic_login_cycle:
            log.warning("automatic_otp_failed message=%s", message)
            self.login_in_progress = False
            self.automatic_login_cycle = False
            self._set_auth_state("signed_out")
            self._background_problem("Код из почты не получен", message)
            self._retry_automatic_login_later(message)
            return
        self._manual_2fa(challenge, message)

    def _schedule_login_retry(self, reason: str | None = None):
        # Deliberately fail closed: automatic retries can request unlimited OTPs.
        self.login_retry_scheduled = False
        self.login_retry_generation += 1
        self.automatic_login_cycle = False
        log.warning("automatic_login_stopped reason=%s", reason or "unknown")
        self.statusBar().showMessage("Автовход остановлен. Повторите вход вручную.", 15000)

    def _run_scheduled_login(self):
        if not self.login_retry_scheduled:
            return
        self.login_retry_scheduled = False
        self._auto_login()

    def _needs_setup(self) -> bool:
        """A fresh installation: nothing to log in with and no group to match rooms by."""
        if self.mirea.session or self.db.get_setting("group", ""):
            return False
        try:
            return not self.session_store.load_credentials()
        except Exception:  # noqa: BLE001 - an unreadable keyring is also "nothing saved"
            return True

    def _first_run_setup(self):
        log.info("first_run_setup_started")
        dialog = SetupDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self.statusBar().showMessage(
                "Настройка не завершена: заполните группу в «Настройках» и нажмите «Войти в MIREA»"
            )
            return
        group = dialog.group.text().strip()
        if group and not is_group_code(group):
            QMessageBox.warning(
                self,
                "Группа",
                f"«{group}» не похоже на номер группы. Ожидается вид ИКБО-01-24. "
                "Продолжить можно, но комната вебинара по такой группе не найдётся.",
            )
        self.db.set_setting("group", group)
        self.db.set_setting("student_name", dialog.student_name.text().strip())
        self.group_edit.setText(group)
        self.student_name.setText(dialog.student_name.text().strip())
        self._update_group_label()
        log.info("first_run_setup_done group_valid=%s", is_group_code(group))
        self.login()

    def _startup_auth(self):
        if os.environ.get("MIREA_ASSISTANT_SMOKE_TEST") == "1":
            # A build smoke test must never reach MIREA and trigger a real 2FA email.
            log.info("startup_auth_skipped reason=smoke_test")
            return
        if self._needs_setup():
            self._first_run_setup()
            return
        # A valid saved session must not generate a fresh OTP on every launch.
        if self.mirea.session:
            self._set_auth_state("checking")
            self._verify_stored_session("Проверяем сохранённый вход…")
        else:
            self._auto_login()

    def _verify_stored_session(self, busy_text: str):
        session = self.mirea.session
        self._run(
            lambda: run_async(self.mirea.verify_state()),
            lambda state: self._session_verified(state, session),
            busy_text,
            lambda _message: self._session_verified(SessionState.UNKNOWN, session),
        )

    def _session_verified(self, state: SessionState, session: dict | None = None):
        if session is not None and (session is not self.mirea.session or self.login_in_progress):
            # A new login replaced (or is replacing) the session this was about.
            log.info("stored_session_verdict_dropped state=%s", state.value)
            return
        log.info("stored_session_verified state=%s", state.value)
        if state is SessionState.EXPIRED:
            # A kept stale session makes the background schedule refresh fail and
            # start a recovery login on top of this one.
            self._discard_expired_session()
            self._set_auth_state("expired")
            self._auto_login()
            return
        if state is SessionState.UNKNOWN:
            # The saved session is kept and used; only the check itself failed.
            self._set_auth_state("checking")
            self.statusBar().showMessage("MIREA пока недоступна; повторим проверку", 8000)
            if not self.session_recheck_scheduled:
                self.session_recheck_scheduled = True
                QTimer.singleShot(60_000, self._retry_session_verification)
            # The check may fail where the schedule itself still loads.
            self._refresh_schedule_background()
            return
        if self.pending_pulse_login is not None:
            result = self.pending_pulse_login
            result.success = True
            result.cookies = dict(self.mirea.session)
            self._login_finished(result)
            return
        self._set_auth_state("signed_in")
        self.statusBar().showMessage("Сохранённый вход восстановлен", 3000)
        self._persist_session()
        self.refresh_schedule()
        for event_id in tuple(self.pending_qr):
            if event_id not in self.retry_scheduled:
                self._retry_attendance(event_id)

    def _retry_session_verification(self):
        if not self.session_recheck_scheduled:
            return
        self.session_recheck_scheduled = False
        if self.login_in_progress or not self.mirea.session:
            return
        self._verify_stored_session("Повторно проверяем MIREA…")

    def _retry_login_now(self):
        """Sign in again at once, the way automatic sign-in does: no dialog, no code to type."""
        if self.login_in_progress:
            self.statusBar().showMessage("Вход уже выполняется — дождитесь его окончания", 6000)
            return
        try:
            credentials = self.session_store.load_credentials()
        except Exception:  # noqa: BLE001 - keyring backends fail in many ways
            credentials = None
        if not credentials:
            # Nothing saved to sign in with: the usual dialog asks once and saves.
            self.login()
            return
        log.info("login_retry_requested_by_user")
        # A retry scheduled for later is replaced by this one.
        self.login_retry_generation += 1
        self.login_retry_scheduled = False
        self.login_retry_attempt = 0
        self._set_auth_state("checking")
        self._auto_login(requested=True)

    def _auto_login(self, requested: bool = False):
        if self.login_in_progress:
            return
        if not requested and not bool(self.db.get_setting("auto_login", True)):
            self.statusBar().clearMessage()
            return
        try:
            credentials = self.session_store.load_credentials()
            email_credentials = self.session_store.load_email_credentials()
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"Не удалось открыть защищённое хранилище: {exc}", 6000)
            self._schedule_login_retry(str(exc))
            return
        if not credentials:
            self.statusBar().clearMessage()
            return
        if not self.db.reserve_auth_attempt("mirea"):
            log.warning("automatic_login_blocked reason=cooldown")
            self.statusBar().showMessage(
                "Автовход ограничен: не более 5 попыток за 30 минут. Войдите вручную.", 15000
            )
            if requested:
                self._set_auth_state("signed_out")
            self._retry_automatic_login_later("Лимит попыток входа; ждём снятия ограничения")
            return
        self.pending_login_credentials = credentials
        self.pending_email_credentials = email_credentials
        self.automatic_login_cycle = True
        self.remember_login_requested = True
        self.login_started_at = datetime.now(UTC)
        self.otp_submission_attempted = False
        self.login_cycle_retries = 0
        username, password = credentials
        self._run_initial_login(username, password, "Входим в MIREA автоматически…")

    def refresh_schedule(self):
        if not self.mirea.session:
            QMessageBox.information(self, "Расписание", "Сначала войдите в MIREA.")
            return
        if self.schedule_refresh_running:
            return
        self.schedule_refresh_running = True
        self._run(
            lambda: run_async(self.mirea.get_schedule(14)),
            self._schedule_loaded,
            "Обновляем расписание…",
            failed=self._schedule_refresh_failed,
        )

    def _refresh_schedule_background(self):
        if not self.mirea.session or self.schedule_refresh_running:
            return
        self.schedule_refresh_running = True
        self._run(
            lambda: run_async(self.mirea.get_schedule(14)),
            self._schedule_loaded,
            "Проверяем, не появилась ли лекция…",
            failed=self._schedule_refresh_failed,
        )

    def _session_is_fresh(self) -> bool:
        obtained = self.session_obtained_at
        return obtained is not None and time.monotonic() - obtained < FRESH_SESSION_SECONDS

    def _schedule_refresh_failed(self, message: str):
        self.schedule_refresh_running = False
        # A failed refresh may still have renewed the tokens on the way.
        self._persist_session()
        log.warning("schedule_refresh_failed message=%s", message)
        self.statusBar().showMessage(
            "Расписание пока недоступно; повторим проверку через минуту: " + message,
            6000,
        )
        if self._hard_relogin_due(message):
            self._hard_relogin(message)
        else:
            self._recover_expired_session("schedule_refresh")
        # A room already found needs no Pulse session to be opened.
        self._evaluate_current_lessons(self.db.list_lessons())

    def _hard_relogin_due(self, message: str) -> bool:
        """Whether the schedule has failed long enough, for reasons a login might fix."""
        now = time.monotonic()
        if is_network_trouble(message):
            self.schedule_failing_since = None
            return False
        if self.schedule_failing_since is None:
            self.schedule_failing_since = now
            return False
        last = self.last_hard_relogin
        return (
            now - self.schedule_failing_since >= HARD_RELOGIN_AFTER_SECONDS
            and (last is None or now - last >= HARD_RELOGIN_EVERY_SECONDS)
            and not self.login_in_progress
        )

    def _hard_relogin(self, reason: str):
        """Sign out of MIREA completely and sign in again, when nothing else helped."""
        try:
            credentials = self.session_store.load_credentials()
        except Exception:  # noqa: BLE001 - without them there is nothing to sign in with
            credentials = None
        if not credentials or not bool(self.db.get_setting("auto_login", True)):
            return  # never sign out a session that cannot be replaced automatically
        self.last_hard_relogin = time.monotonic()
        self.schedule_failing_since = None
        log.warning("hard_relogin reason=%s", reason)
        self.statusBar().showMessage(
            "Расписание не загружается 20 минут — выходим из MIREA и входим заново", 10000
        )

        def sign_in_again(_result=None):
            self._discard_expired_session()
            self._set_auth_state("expired")
            self._auto_login()

        self._run(
            lambda: run_async(self.mirea.logout()),
            sign_in_again,
            "Выходим из MIREA…",
            failed=lambda _message: sign_in_again(),
        )

    def _recover_expired_session(self, reason: str):
        """Re-enter automatically when a saved session expires while the app is running."""
        if self.login_in_progress or self.auth_recovery_running or not self.mirea.session:
            return
        if self._session_is_fresh():
            log.info("session_recovery_skipped reason=fresh_session trigger=%s", reason)
            return
        self.auth_recovery_running = True
        log.info("session_recovery_check reason=%s", reason)
        session = self.mirea.session

        def checked(state: SessionState):
            self.auth_recovery_running = False
            if session is not self.mirea.session or self.login_in_progress:
                log.info("session_recovery_verdict_dropped state=%s", state.value)
                return
            if state is SessionState.VALID:
                self._persist_session()
                return
            if state is SessionState.UNKNOWN:
                log.info("session_recovery_deferred reason=network")
                return
            log.warning("session_expired reason=%s", reason)
            self._discard_expired_session()
            self._set_auth_state("expired")
            self._auto_login()

        def failed(message: str):
            self.auth_recovery_running = False
            log.warning("session_recovery_check_failed reason=%s message=%s", reason, message)

        self._run(
            lambda: run_async(self.mirea.verify_state()),
            checked,
            "Проверяем вход в MIREA…",
            failed=failed,
        )

    def _schedule_loaded(self, lessons):
        self.schedule_refresh_running = False
        self.schedule_failing_since = None
        if self.pending_pulse_login is not None:
            result = self.pending_pulse_login
            result.success = True
            result.cookies = dict(self.mirea.session)
            self._login_finished(result, refresh_schedule=False)
        self._set_auth_state("signed_in")
        # pymirea renewed the cookie or the tokens on the way; without saving them
        # the next start used the spent ones and asked for a new login and code.
        self._persist_session()
        today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        missing, dropped = self.db.sync_lessons(lessons, today)
        self._fill_schedule()
        self.statusBar().showMessage(f"Загружено занятий: {len(lessons)}", 4000)
        log.info(
            "schedule_loaded lesson_count=%s missing=%s dropped=%s",
            len(lessons),
            missing,
            dropped,
        )
        # The merged cache, not the raw reply: a day Pulse failed to return must
        # not hide the pair that is on right now.
        self._evaluate_current_lessons(self.db.list_lessons())

    def _superseded(self, lesson, now: datetime, lessons=None) -> bool:
        """Whether a later pair now owns the tab instead of ``lesson``.

        A pair keeps the tab until five minutes past its end; the next one takes
        it then, or as soon as it has actually begun. Without this an earlier
        pair whose room had closed at the bell reopened that dead room on top of
        the next pair and the next pair was lost.
        """
        lead = timedelta(minutes=self.join_before.value())
        for other in lessons if lessons is not None else self.db.list_lessons():
            if other.external_id == lesson.external_id or other.start_at <= lesson.start_at:
                continue
            if now >= other.start_at or (
                now >= other.start_at - lead and now >= lesson.end_at + LEAVE_AFTER_END
            ):
                return True
        return False

    def _lesson_of(self, lesson_id: str | None):
        if not lesson_id:
            return None
        if self.active_lesson is not None and self.active_lesson.external_id == lesson_id:
            return self.db.get_lesson(lesson_id) or self.active_lesson
        return self.db.get_lesson(lesson_id)

    def _adopt_running_room(self, lesson):
        """A double pair in one room: the next pair takes over the room already open."""
        previous = self.active_lecture_id
        url = self.active_lecture_url or ""
        log.info("lecture_room_adopted lesson_id=%s from=%s", lesson.external_id, previous)
        self.db.set_resolved_link(lesson.external_id, url)
        self._clear_pending_for_lesson(previous)
        self.room_lost_lessons.discard(previous or "")
        self.joined_lessons.add(lesson.external_id)
        self._make_active(lesson.external_id)
        marked = self._attendance_already_marked(lesson.external_id)
        if not marked and not self.scan_timer.isActive():
            self.toggle_scanner()
        self.statusBar().showMessage(
            f"«{lesson.subject_name}»: пара идёт в той же комнате — ищем её QR", 8000
        )

    def _evaluate_cached_lessons(self):
        self._evaluate_current_lessons(self.db.list_lessons())

    def _restore_active_lecture(self):
        """Resume only a room actually monitored before a crash/update, not old links."""
        saved = self.db.get_setting("active_lecture", {})
        if not saved or self.active_lecture_id or self.opening_lecture_id:
            return
        lesson = self.db.get_lesson(saved.get("lesson_id"))
        if lesson is None and isinstance(saved.get("lesson"), dict):
            try:
                snapshot = dict(saved["lesson"])
                snapshot["start_at"] = datetime.fromisoformat(snapshot["start_at"])
                snapshot["end_at"] = datetime.fromisoformat(snapshot["end_at"])
                lesson = Lesson(**snapshot)
                if (
                    lesson.external_id != saved.get("lesson_id")
                    or lesson.start_at.tzinfo is None
                    or lesson.end_at.tzinfo is None
                ):
                    lesson = None
            except (KeyError, ValueError, TypeError):
                lesson = None
        now = datetime.now().astimezone()
        if (
            lesson is None
            or not saved.get("url")
            or not (lesson.start_at <= now <= lesson.end_at + timedelta(hours=2))
            or self.db.get_rule(lesson.subject_name) is RuleMode.IGNORE
            or self._superseded(lesson, now)
        ):
            self.db.set_setting("active_lecture", {})
            return
        self.active_lecture_url = saved["url"]
        self.active_lesson = lesson
        self._make_active(lesson.external_id)
        log.info("lecture_resume_requested lesson_id=%s", lesson.external_id)
        self._open_lecture(saved["url"], lesson.external_id, force=True)

    def _is_current(self, lesson, now: datetime, lead: timedelta | None = None) -> bool:
        lead = timedelta(minutes=self.join_before.value()) if lead is None else lead
        return lesson.start_at - lead <= now <= lesson.end_at + LEAVE_AFTER_END

    def _earlier_pair_holds_tab(self, lesson, now: datetime) -> bool:
        """An earlier pair still on: in its room, or looking for a new one."""
        for other_id in {self.active_lecture_id, *self.room_lost_lessons} - {None}:
            if other_id == lesson.external_id:
                continue
            other = self._lesson_of(other_id)
            if (
                other is not None
                and other.start_at < lesson.start_at
                and not self._superseded(other, now)
            ):
                return True
        return False

    @staticmethod
    def _in_person(lesson) -> bool:
        """A pair with a lecture room of its own, not «Дистанционно» or the СДО."""
        room = (lesson.room or "").strip().casefold()
        return (
            bool(room)
            and not lesson.is_online
            and not any(marker in room for marker in ONLINE_ROOM_MARKERS)
        )

    def _manual_link(self, lesson_id: str) -> str:
        """The room the student typed in for this pair, unless it just turned out closed."""
        url = self.db.get_setting("manual_links", {}).get(lesson_id, "")
        return "" if url in self._rejected_rooms(lesson_id) else url

    def _evaluate_current_lessons(self, lessons):
        now = datetime.now().astimezone()
        lead_minutes = self.join_before.value()
        active = self._lesson_of(self.active_lecture_id)
        # Teachers may keep a room live after the nominal bell. Prefer the newest
        # eligible lesson so an overrun never wins over a pair that has just begun.
        for lesson in sorted(lessons, key=lambda item: item.start_at, reverse=True):
            if not (
                lesson.start_at - timedelta(minutes=lead_minutes)
                <= now
                <= lesson.end_at + timedelta(minutes=90)
            ):
                continue
            mode = self.db.get_rule(lesson.subject_name)
            if lesson.external_id == self.active_lecture_id:
                # Teachers recreate rooms mid-pair; keep an eye on the СДО.
                if now < lesson.end_at and mode is not RuleMode.IGNORE:
                    self._resolve_from_sources(lesson)
                continue
            if self._superseded(lesson, now, lessons):
                # An earlier pair: never again on top of the one that follows it.
                self.room_lost_lessons.discard(lesson.external_id)
                continue
            if active is not None and not self._superseded(active, now, lessons):
                continue  # the running pair keeps the tab until its end + 5 min
            if lesson.external_id in self.joined_lessons:
                continue
            if mode is RuleMode.IGNORE:
                continue
            if self._in_person(lesson) and not self._manual_link(lesson.external_id):
                # A pair in a lecture room has no webinar to look for: the browser
                # is left alone instead of reading the СДО all pair long.
                continue
            if (
                active is not None
                and active.subject_name == lesson.subject_name
                and self.active_lecture_url
                and not self.db.get_resolved_link(lesson.external_id)
            ):
                # A double pair of one subject usually stays in one room, whose
                # СДО row starts with the first pair and never matches the second.
                self._adopt_running_room(lesson)
                break
            url = (
                self.db.get_resolved_link(lesson.external_id)
                or self._manual_link(lesson.external_id)
                or lesson.source_url
            )
            if url and url in self._rejected_rooms(lesson.external_id):
                url = ""
            if not url:
                # The webinar is often created after the pair has begun, so this
                # runs again (with a pause between attempts) until the room shows up.
                if now <= lesson.end_at + LEAVE_AFTER_END + timedelta(minutes=10):
                    self._resolve_from_sources(lesson)
                continue
            if url and not self.active_lecture_id and self.browser.lecture_url == url:
                # The student opened this room by hand well before the pair: monitor
                # it now instead of asking about it or opening it a second time.
                log.info("current_lesson_adopted_open_room lesson_id=%s", lesson.external_id)
                self._lecture_opened("браузере", lesson.external_id)
                break
            if mode is RuleMode.AUTO or lesson.external_id in self.accepted_lessons:
                log.info(
                    "current_lesson_action lesson_id=%s mode=%s action=open",
                    lesson.external_id,
                    mode.value,
                )
                self._open_lecture(url, lesson.external_id)
                break
            if lesson.external_id in self.prompted_lessons:
                continue
            self.prompted_lessons.add(lesson.external_id)
            log.info(
                "current_lesson_action lesson_id=%s mode=ASK action=prompt", lesson.external_id
            )
            if self._ask(
                "Лекция доступна", f"Появилась лекция «{lesson.subject_name}». Открыть её?"
            ):
                self.accepted_lessons.add(lesson.external_id)
                # The answer may come much later: take the room known now.
                fresh = self.db.get_resolved_link(lesson.external_id) or url
                self._open_lecture(fresh, lesson.external_id)
                break

    def _edit_sources(self, subject: str):
        dialog = SourcesDialog(subject, self.db.get_sources(subject), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        urls = dialog.urls()
        self.db.set_sources(subject, urls)
        log.info("lecture_sources_saved subject=%s count=%s", subject, len(urls))
        self.statusBar().showMessage(f"Источников для «{subject}»: {len(urls)}", 4000)

    def _read_source(self, url: str) -> str:
        """Read a СДО page through the app's browser, signing in if it asks."""
        self.browser.ensure_running()
        try:
            return self.browser.read_html(url)
        except NotSignedInError:
            if not self._sign_in_to_sdo():
                raise
            try:
                return self.browser.read_html(url)
            except NotSignedInError:
                # The sign-in "worked" but the СДО still shows its form: back off
                # instead of emailing another code at the next lookup.
                self.sdo_sign_in_failed_at = time.monotonic()
                raise

    def _sign_in_to_sdo(self) -> bool:
        """Sign in to the СДО once at a time, pausing after a failure.

        Every sign-in emails a code and spends the shared login budget. Lookups of
        two lessons used to sign in side by side and pick up each other's codes,
        and a failing sign-in repeated every minute until auto-login was blocked.
        """
        if self._sdo_sign_in_backing_off():
            log.info("sdo_sign_in_skipped reason=backoff")
            return False
        if not self.sdo_sign_in_lock.acquire(blocking=False):
            # Another lookup is signing in right now; wait for it and reuse it.
            log.info("sdo_sign_in_waiting_for_other")
            if not self.sdo_sign_in_lock.acquire(timeout=240):
                return False
            self.sdo_sign_in_lock.release()
            # A deferred or skipped sign-in is no reason to read again and back off.
            return self.sdo_last_sign_in_ok and not self._sdo_sign_in_backing_off()
        try:
            self.sdo_last_sign_in_ok = False
            self.sdo_last_sign_in_ok = self._sign_in_to_sdo_locked()
            return self.sdo_last_sign_in_ok
        except Exception:
            self.sdo_sign_in_failed_at = time.monotonic()
            raise
        finally:
            self.sdo_sign_in_lock.release()

    def _sdo_sign_in_backing_off(self) -> bool:
        failed_at = self.sdo_sign_in_failed_at
        return failed_at is not None and time.monotonic() - failed_at < SDO_SIGN_IN_BACKOFF_SECONDS

    def _sign_in_to_sdo_locked(self) -> bool:
        if self.login_in_progress:
            # Both flows read codes from one mailbox: while the Pulse login
            # waits for its code, an СДО code would be taken for it (or vice versa).
            log.info("sdo_sign_in_deferred reason=pulse_login_in_progress")
            return False
        try:
            credentials = self.session_store.load_credentials()
            email_credentials = self.session_store.load_email_credentials()
        except Exception as exc:  # noqa: BLE001 - keyring backends fail in many ways
            log.warning("sdo_sign_in_no_credentials error=%s", exc)
            return False
        if not credentials:
            log.info("sdo_sign_in_skipped reason=no_credentials")
            return False

        def reserve_attempt() -> bool:
            if self.db.reserve_auth_attempt("sdo"):
                return True
            log.warning("sdo_sign_in_blocked reason=cooldown")
            return False

        requested_at = datetime.now(UTC)
        latest_uid = None
        if email_credentials:
            try:
                latest_uid = self.otp_reader.latest_uid(email_credentials)
            except Exception as exc:  # noqa: BLE001 - a snapshot is an optimisation
                log.warning("sdo_sign_in_uid_snapshot_failed error=%s", exc)

        def request_code() -> str:
            if not email_credentials:
                raise SignInFailed("Для входа в СДО нужен код, а почта не настроена")
            code = self.otp_reader.wait_for_code(
                email_credentials, requested_at, after_uid=latest_uid
            )
            self.used_codes.add(code)
            return code

        username, password = credentials
        log.info("sdo_sign_in_started")
        try:
            self.browser.run_on_new_page(
                lambda page: sign_in(
                    page,
                    username=username,
                    password=password,
                    request_code=request_code,
                    reserve_attempt=reserve_attempt,
                )
            )
        except Exception:
            # Its letter may still arrive after a failed sign-in.
            self._claim_own_codes()
            raise
        return True

    def _resolve_from_sources(self, lesson):
        if lesson.external_id in self.resolving_lessons:
            return
        if self._superseded(lesson, datetime.now().astimezone()):
            return
        if time.monotonic() < self.lookup_not_before.get(lesson.external_id, 0.0):
            return
        group = self.group_edit.text().strip()
        saved = self.db.get_sources(lesson.subject_name)
        excluded = self._rejected_rooms(lesson.external_id)
        self.resolving_lessons.add(lesson.external_id)
        log.info(
            "webinar_lookup_started lesson_id=%s saved_sources=%s",
            lesson.external_id,
            len(saved),
        )

        def lookup():
            # Always refresh the student's current course list. Previously a stale
            # saved result permanently hid courses added later in the semester.
            discovered = discover_course_urls(self._read_source, lesson.subject_name)
            # Current enrolments go first; old saved sources must not consume the
            # page budget before the current semester is examined.
            sources = list(dict.fromkeys([*discovered, *saved]))
            webinar = resolve_lecture_url(
                self._read_source,
                sources,
                subject=lesson.subject_name,
                start_at=lesson.start_at,
                end_at=lesson.end_at,
                group=group,
                teacher=lesson.teacher,
                # Rooms whose scheduled end has passed are over, not the one to join.
                now=datetime.now().astimezone(),
                excluded=excluded,
            )
            # Automatically discovered courses are refreshed each pass. Do not
            # mix them permanently with explicitly configured manual sources.
            return webinar, []

        started = time.monotonic()
        self._run(
            lookup,
            lambda payload: self._source_resolved(lesson, *payload, started=started),
            "Ищем вебинар в СДО…",
            failed=lambda message: self._source_lookup_failed(lesson, message, started=started),
        )

    def _lookup_pause(self, lesson) -> float:
        """Seconds until the next СДО lookup for this lesson."""
        if lesson.external_id in self.room_lost_lessons:
            return ROOM_LOST_RECHECK_SECONDS
        if lesson.external_id == self.active_lecture_id:
            now = datetime.now().astimezone()
            early = now < lesson.start_at + RECHECK_EARLY_WINDOW
            return RECHECK_EARLY_SECONDS if early else RECHECK_LATE_SECONDS
        return LOOKUP_RETRY_EMPTY_SECONDS

    def _source_resolved(
        self, lesson, webinar, discovered: list[str], started: float | None = None
    ):
        self.resolving_lessons.discard(lesson.external_id)
        # Counted from the start: a slow СДО stretched "every 2 minutes" to 3 or 4.
        begun = time.monotonic() if started is None else started
        self.lookup_not_before[lesson.external_id] = begun + self._lookup_pause(lesson)
        if webinar is not None and webinar.join_url:
            if webinar.webinar_id is not None:
                self.room_webinar_ids[webinar.join_url] = webinar.webinar_id
            if webinar.join_url in self._rejected_rooms(lesson.external_id):
                # The lookup began before this room was found closed.
                webinar = None
        if self._superseded(lesson, datetime.now().astimezone()):
            log.info("webinar_lookup_dropped lesson_id=%s reason=superseded", lesson.external_id)
            return
        if webinar is None or not webinar.is_joinable:
            if lesson.external_id == self.active_lecture_id:
                return  # still in the room we have; nothing newer yet
            legacy = self.db.get_link(lesson.subject_name)
            if legacy and legacy not in self._rejected_rooms(lesson.external_id):
                # A permanent room set for the whole subject by an older version:
                # used only when the СДО has nothing for this particular lesson.
                self._room_found(lesson, legacy)
                return
        if discovered and discovered != self.db.get_sources(lesson.subject_name):
            self.db.set_sources(lesson.subject_name, discovered)
            log.info(
                "lecture_sources_discovered subject=%s count=%s",
                lesson.subject_name,
                len(discovered),
            )
        if webinar is None or not webinar.is_joinable:
            log.info(
                "webinar_lookup_empty lesson_id=%s found=%s",
                lesson.external_id,
                bool(webinar),
            )
            # Silence here reads as "the app is broken"; say what was actually seen.
            self.statusBar().showMessage(
                f"«{lesson.subject_name}»: вебинар найден, но комната ещё не открыта — ждём"
                if webinar
                else f"«{lesson.subject_name}»: вебинар в СДО пока не появился — ждём",
                8000,
            )
            return
        manual = self._manual_link(lesson.external_id)
        if lesson.external_id == self.active_lecture_id:
            if webinar.join_url == self.active_lecture_url:
                return
            if self.db.get_rule(lesson.subject_name) is RuleMode.IGNORE:
                return
            if manual and manual == self.active_lecture_url:
                return  # the student's own room stays until it closes
            current_id = self.room_webinar_ids.get(self.active_lecture_url or "")
            if current_id is not None and (webinar.webinar_id or 0) <= current_id:
                # Only a newer room replaces the one we are in; an older row
                # coming back after its rejection lapsed must not pull us out.
                return
            # A newer room for this pair: the teacher closed the first one.
            log.warning(
                "lecture_room_replaced lesson_id=%s webinar_id=%s",
                lesson.external_id,
                webinar.webinar_id,
            )
            self.db.set_resolved_link(lesson.external_id, webinar.join_url)
            self.statusBar().showMessage(
                f"«{lesson.subject_name}»: в СДО новая комната — переходим в неё", 8000
            )
            self._open_lecture(webinar.join_url, lesson.external_id, force=True)
            return
        if manual and manual != webinar.join_url:
            # The student's own room goes first while it is not known to be closed.
            log.info("webinar_resolved_kept_manual lesson_id=%s", lesson.external_id)
            return
        self.room_lost_lessons.discard(lesson.external_id)
        self.db.set_resolved_link(lesson.external_id, webinar.join_url)
        log.info(
            "webinar_resolved lesson_id=%s title=%s start=%s end=%s groups=%s",
            lesson.external_id,
            webinar.title[:80],
            f"{webinar.start_at:%d.%m %H:%M}",
            f"{webinar.end_at:%H:%M}" if webinar.end_at else "-",
            ",".join(webinar.groups) or "-",
        )
        self._room_found(lesson, webinar.join_url)

    def _room_found(self, lesson, url: str):
        self._fill_schedule()
        self.statusBar().showMessage(f"Вебинар найден: {lesson.subject_name}", 5000)
        mode = self.db.get_rule(lesson.subject_name)
        if mode is RuleMode.AUTO or (
            mode is RuleMode.ASK and lesson.external_id in self.accepted_lessons
        ):
            self._open_lecture(url, lesson.external_id)
        elif mode is RuleMode.ASK and lesson.external_id not in self.prompted_lessons:
            self.prompted_lessons.add(lesson.external_id)
            if self._ask(
                "Вебинар найден", f"В СДО появился вебинар «{lesson.subject_name}». Открыть?"
            ):
                self.accepted_lessons.add(lesson.external_id)
                fresh = self.db.get_resolved_link(lesson.external_id) or url
                self._open_lecture(fresh, lesson.external_id)

    def _source_lookup_failed(self, lesson, message: str, started: float | None = None):
        self.resolving_lessons.discard(lesson.external_id)
        begun = time.monotonic() if started is None else started
        # A pair in progress keeps its own cadence; otherwise back off further.
        in_progress = lesson.external_id in self.room_lost_lessons or (
            lesson.external_id == self.active_lecture_id
        )
        pause = self._lookup_pause(lesson) if in_progress else LOOKUP_RETRY_FAILED_SECONDS
        self.lookup_not_before[lesson.external_id] = begun + pause
        log.warning("webinar_lookup_failed lesson_id=%s message=%s", lesson.external_id, message)
        self.statusBar().showMessage("Вебинар в СДО пока не найден: " + message, 6000)

    def refresh_views(self):
        self._fill_schedule()
        self._fill_history()

    def _fill_schedule(self):
        self._update_now_card()
        focused = QApplication.focusWidget()
        if (
            self.isActiveWindow()
            and focused is not None
            and focused is not self.schedule_table
            and self.schedule_table.isAncestorOf(focused)
        ):
            # The minute tick rebuilds every cell widget. Doing that while a link is
            # being typed destroys the editor before editingFinished can save it.
            log.debug("schedule_redraw_deferred reason=cell_editing")
            if not self.schedule_redraw_pending:
                self.schedule_redraw_pending = True
                QTimer.singleShot(3_000, self._deferred_fill_schedule)
            return
        lessons = self.db.list_lessons()
        rules = self.db.all_rules()
        resolved_links = self.db.all_resolved_links()
        subject_links = self.db.all_links()
        signature = (
            tuple(lessons),
            tuple(sorted((key, value.value) for key, value in rules.items())),
            tuple(sorted(resolved_links.items())),
            tuple(sorted(subject_links.items())),
            self.db.get_setting("schedule_lesson_type", ""),
        )
        if signature == self.schedule_signature:
            return
        self.schedule_signature = signature
        subjects = sorted({lesson.subject_name for lesson in lessons})
        selected_subject = self.subject_rule_subject.currentText()
        self.subject_rule_subject.blockSignals(True)
        self.subject_rule_subject.clear()
        self.subject_rule_subject.addItems(subjects)
        if selected_subject in subjects:
            self.subject_rule_subject.setCurrentText(selected_subject)
        self.subject_rule_subject.blockSignals(False)
        self._selected_subject_changed(self.subject_rule_subject.currentText())
        available_types = sorted(
            {lesson.lesson_type.strip() for lesson in lessons if lesson.lesson_type.strip()}
        )
        selected_type = self.db.get_setting("schedule_lesson_type", "")
        self.lesson_type_filter.blockSignals(True)
        self.lesson_type_filter.clear()
        self.lesson_type_filter.addItem("Все типы", "")
        for lesson_type in available_types:
            self.lesson_type_filter.addItem(lesson_type, lesson_type)
        selected_index = self.lesson_type_filter.findData(selected_type)
        self.lesson_type_filter.setCurrentIndex(max(0, selected_index))
        self.lesson_type_filter.blockSignals(False)
        if selected_type in available_types:
            lessons = [lesson for lesson in lessons if lesson.lesson_type == selected_type]
        self.schedule_table.setRowCount(len(lessons))
        for row, lesson in enumerate(lessons):
            values = (
                lesson.start_at.strftime("%d.%m.%Y"),
                f"{lesson.start_at:%H:%M}–{lesson.end_at:%H:%M}",
                lesson.subject_name,
                lesson.lesson_type,
                lesson.teacher or "—",
            )
            now = datetime.now().astimezone()
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if column == 2:
                    cell.setToolTip(
                        f"{lesson.subject_name}\nПреподаватель: {lesson.teacher or 'не указан'}"
                    )
                if lesson.end_at < now:
                    cell.setForeground(QBrush(QColor(self.colors["past"])))
                elif lesson.start_at <= now:
                    cell.setBackground(QBrush(QColor(self.colors["current"])))
                    cell.setToolTip(
                        "Идёт сейчас\n" + cell.toolTip() if cell.toolTip() else "Идёт сейчас"
                    )
                self.schedule_table.setItem(row, column, cell)
            mode = rules.get(lesson.subject_name, RuleMode.ASK)
            mode_item = QTableWidgetItem(MODE_LABELS[mode])
            mode_item.setBackground(QBrush(QColor(self.colors[MODE_TOKENS[mode]])))
            mode_item.setToolTip("Общее правило предмета; изменяется над таблицей")
            mode_item.setFlags(mode_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.schedule_table.setItem(row, 5, mode_item)
            found = resolved_links.get(lesson.external_id)
            link = QLineEdit(
                found or lesson.source_url or subject_links.get(lesson.subject_name, "")
            )
            link.setToolTip(
                "Адрес комнаты этого занятия"
                if found
                else "Вставьте адрес комнаты; он сохранится только для этого занятия"
            )
            link.setPlaceholderText("https://…")
            link.setCursorPosition(0)
            # Saved per lesson: a subject-wide link reopened last week's room for
            # every later pair and stopped the СДО lookup for good.
            shown_link = link.text().strip()
            link.editingFinished.connect(
                lambda lesson_id=lesson.external_id, editor=link, shown=shown_link: (
                    self._save_lesson_link(lesson_id, editor.text().strip(), shown)
                )
            )
            self.schedule_table.setCellWidget(row, 6, link)
            open_button = QPushButton("Открыть", objectName="secondary")
            open_button.clicked.connect(
                lambda _=False, editor=link, lesson_id=lesson.external_id: self._open_lecture(
                    editor.text().strip(), lesson_id, force=True, manual=True
                )
            )
            self.schedule_table.setCellWidget(row, 7, open_button)
            sources_button = QPushButton("Источник", objectName="secondary")
            sources_button.setToolTip("Страницы СДО, где искать вебинар этого предмета")
            sources_button.clicked.connect(
                lambda _=False, subject=lesson.subject_name: self._edit_sources(subject)
            )
            self.schedule_table.setCellWidget(row, 8, sources_button)

    def _deferred_fill_schedule(self):
        self.schedule_redraw_pending = False
        self._fill_schedule()

    def _save_lesson_link(self, lesson_id: str, url: str, shown: str | None = None):
        if url == shown:
            return  # focus merely left the field
        if not url:
            if shown:
                # Cleared by the student: unpin the room, the СДО decides again.
                manual = self.db.get_setting("manual_links", {})
                if manual.pop(lesson_id, None) is not None:
                    self.db.set_setting("manual_links", manual)
                self.db.forget_resolved_link(lesson_id)
                log.info("lesson_link_cleared lesson_id=%s", lesson_id)
            return
        if url and url != self.db.get_resolved_link(lesson_id):
            self.db.set_resolved_link(lesson_id, url)
            # Remembered as the student's own: СДО rechecks used to replace it.
            known = {lesson.external_id for lesson in self.db.list_lessons()}
            manual = {
                key: value
                for key, value in self.db.get_setting("manual_links", {}).items()
                if key in known
            }
            manual[lesson_id] = url
            self.db.set_setting("manual_links", manual)
            # A link the student typed in is trusted even if a room there ended before.
            rejected = self.db.get_setting("rejected_rooms", {})
            if url in rejected.get(lesson_id, {}):
                del rejected[lesson_id][url]
                self.db.set_setting("rejected_rooms", rejected)
            log.info("lesson_link_saved lesson_id=%s", lesson_id)

    def _schedule_filter_changed(self, index: int):
        lesson_type = self.lesson_type_filter.itemData(index) or ""
        self.db.set_setting("schedule_lesson_type", lesson_type)
        self._fill_schedule()

    def _set_subject_rule(self, subject: str, mode: RuleMode):
        """Apply one choice to every visible and future lesson of the subject."""
        self.db.set_rule(subject, mode)
        for row in range(self.schedule_table.rowCount()):
            item = self.schedule_table.item(row, 2)
            mode_item = self.schedule_table.item(row, 5)
            if item is not None and item.text() == subject and mode_item is not None:
                mode_item.setText(MODE_LABELS[mode])
                mode_item.setBackground(QBrush(QColor(self.colors[MODE_TOKENS[mode]])))
        self._update_now_card()
        self.statusBar().showMessage(
            f"Для всех занятий «{subject}» установлен режим «{MODE_LABELS[mode]}»", 4000
        )

    def _selected_subject_changed(self, subject: str):
        if not subject:
            self.subject_rule_mode.setEnabled(False)
            return
        self.subject_rule_mode.setEnabled(True)
        self.subject_rule_mode.blockSignals(True)
        index = self.subject_rule_mode.findData(self.db.get_rule(subject).value)
        self.subject_rule_mode.setCurrentIndex(max(index, 0))
        self.subject_rule_mode.blockSignals(False)

    def _selected_rule_changed(self, index: int):
        subject = self.subject_rule_subject.currentText()
        value = self.subject_rule_mode.itemData(index)
        if subject and value:
            self._set_subject_rule(subject, RuleMode(value))

    def _open_lecture(
        self,
        url: str,
        lesson_id: str | None = None,
        *,
        force: bool = False,
        manual: bool = False,
    ):
        if not url:
            QMessageBox.information(
                self, "Ссылка не указана", "Сначала вставьте ссылку на лекцию в таблице."
            )
            return
        if not manual and not self._may_open(url, lesson_id):
            return
        if lesson_id and (lesson_id, url) in self.room_lost_notified:
            # This address showed an ended room: reload it, as the tab may still be
            # on that ended page and would otherwise be taken as already open.
            force = True
        if lesson_id and (
            lesson_id == self.opening_lecture_id or (lesson_id in self.joined_lessons and not force)
        ):
            return
        self.opening_lecture_id = lesson_id
        muted = self.mute_lecture.isChecked()
        compact = self.compact_window.isChecked()
        self._run(
            lambda: self.browser.open(
                url,
                muted=muted,
                width=520 if compact else 1100,
                height=360 if compact else 760,
                force_navigation=force,
            ),
            lambda browser_name: self._lecture_opened(browser_name, lesson_id, manual=manual),
            "Открываем лекцию…",
            failed=lambda message: self._lecture_open_failed(lesson_id, message),
        )

    def _may_open(self, url: str, lesson_id: str | None) -> bool:
        """The last word before an automatic open: right pair, right time, live room."""
        lesson = self._lesson_of(lesson_id)
        if lesson is None:
            return True
        now = datetime.now().astimezone()
        reason = None
        if now > lesson.end_at + LEAVE_AFTER_END and lesson_id != self.active_lecture_id:
            reason = "pair_over"
        elif self._superseded(lesson, now):
            reason = "superseded"
        elif url in self._rejected_rooms(lesson.external_id):
            reason = "room_rejected"
        elif self._earlier_pair_holds_tab(lesson, now):
            # The earlier pair keeps the tab until its end + 5 minutes, also while
            # it looks for a new room; the next pair used to take it at once.
            reason = "earlier_pair_running"
        if reason:
            log.info("lecture_open_refused lesson_id=%s reason=%s", lesson_id, reason)
            return False
        return True

    def _make_active(self, lesson_id: str | None):
        """Point the monitoring (QR, chat fallback, watchdog) at this lesson."""
        if lesson_id:
            self.active_lesson = self.db.get_lesson(lesson_id) or (
                self.active_lesson
                if self.active_lesson and self.active_lesson.external_id == lesson_id
                else None
            )
        if lesson_id != self.active_lecture_id:
            self.lecture_inactive_checks = 0
            self.active_lecture_id = lesson_id
            sent_lessons = self.db.get_setting("chat_sent_lessons", [])
            self.unreadable_chat_sent = bool(lesson_id and lesson_id in sent_lessons)
            qr_seen_lessons = self.db.get_setting("qr_seen_lessons", [])
            self.qr_detected_in_lecture = bool(lesson_id and lesson_id in qr_seen_lessons)
            self.chat_config_warned = False
            self.chat_baseline = None
            self.lecture_started_at = time.monotonic()
            self.last_capture_at = 0.0
        if lesson_id and self.active_lecture_url:
            checkpoint = {"lesson_id": lesson_id, "url": self.active_lecture_url}
            if self.active_lesson is not None:
                lesson = self.active_lesson
                checkpoint["lesson"] = {
                    "external_id": lesson.external_id,
                    "subject_name": lesson.subject_name,
                    "lesson_type": lesson.lesson_type,
                    "start_at": lesson.start_at.isoformat(),
                    "end_at": lesson.end_at.isoformat(),
                }
            self.db.set_setting("active_lecture", checkpoint)

    def _lecture_opened(self, browser_name: str, lesson_id: str | None, *, manual: bool = False):
        self.opening_lecture_id = None
        lesson = self._lesson_of(lesson_id)
        now = datetime.now().astimezone()
        if manual and lesson is not None and not self._is_current(lesson, now, MANUAL_OPEN_LEAD):
            # A future or past pair opened by hand: its QR codes and marks must not
            # be booked to it, and the pairs of now keep their own monitoring.
            log.info("manual_open_not_monitored lesson_id=%s", lesson_id)
            lesson_id = None
        previous = self.active_lecture_id
        if previous and lesson_id and previous != lesson_id:
            # The tab now shows another pair's room; this one may be opened again.
            self.joined_lessons.discard(previous)
        if lesson_id:
            self.joined_lessons.add(lesson_id)
        self.active_lecture_url = self.browser.lecture_url
        log.info("lecture_opened lesson_id=%s browser=%s", lesson_id, browser_name)
        self._make_active(lesson_id)
        self.statusBar().showMessage(
            f"Страница лекции загружена в {browser_name}; подключаемся…", 4000
        )
        if self.minimize_on_open.isChecked():
            self._minimize_lecture("Сворачиваем окно лекции…")
        self._enter_lecture_room()
        if self._attendance_already_marked(lesson_id):
            log.info("scanner_not_started reason=already_marked lesson_id=%s", lesson_id)
            self.statusBar().showMessage("Посещение уже отмечено, сканирование не нужно", 6000)
            return
        if not self.scan_timer.isActive():
            self.toggle_scanner()

    def _lecture_watch_tick(self):
        if (
            not self.active_lecture_id
            or not self.active_lecture_url
            or self.opening_lecture_id
            or self.lecture_health_check_running
        ):
            return
        lesson = self.db.get_lesson(self.active_lecture_id)
        if lesson is not None:
            self.active_lesson = lesson
        elif self.active_lesson and self.active_lesson.external_id == self.active_lecture_id:
            # Missing from the schedule for now (a Pulse outage): keep watching the
            # room; it ends on its own "ended" banner or the safety timeout.
            lesson = self.active_lesson
        if lesson is not None and datetime.now().astimezone() > lesson.end_at + timedelta(hours=2):
            self._finish_active_lecture("safety_timeout")
            return
        self.lecture_health_check_running = True
        watched = (self.active_lecture_id, self.active_lecture_url)

        def checked(state: str):
            self.lecture_health_check_running = False
            if (self.active_lecture_id, self.active_lecture_url) != watched:
                # The room changed while this check ran; it says nothing about the new one.
                log.info("lecture_health_result_dropped state=%s", state)
                return
            if state == "inactive":
                self.lecture_inactive_checks += 1
                if self.lecture_inactive_checks < 2:
                    return
                if lesson and datetime.now().astimezone() >= lesson.end_at + LEAVE_AFTER_END:
                    self._finish_active_lecture("ended_landing_page")
                    return
                # Before the end it may be a room that has not started yet: reopen
                # it as rarely as a stalled stream, not at every check.
                state = "stalled"
            else:
                self.lecture_inactive_checks = 0
            if state == "unstable":
                # A reconnect banner is usually gone within seconds: act only if
                # it is still there at the next check.
                self.lecture_unstable_checks += 1
                if self.lecture_unstable_checks < 2:
                    return
                state = "lost"
            else:
                self.lecture_unstable_checks = 0
            if state == "live" and (
                self.scan_timer.isActive()
                and time.monotonic() - (self.last_capture_at or self.lecture_started_at) > 30
            ):
                log.warning("lecture_capture_stale")
                state = "stalled"
            if state == "stalled":
                if not self._soft_recovery_due():
                    return
                state = "lost"
            if state == "live":
                self.lecture_recovery_failures = 0
                return
            if state == "ended":
                self._room_ended(lesson)
                return
            if state == "waiting":
                # The room is up but we are still in its lobby; pressing the
                # platform's own control is the only way in.
                self.lecture_recovery_failures = 0
                self._enter_lecture_room()
                return
            self.lecture_recovery_failures += 1
            log.warning("lecture_tab_lost lesson_id=%s", self.active_lecture_id)
            self.statusBar().showMessage("Вкладка лекции отключилась — открываем заново…", 5000)
            if self.lecture_recovery_failures >= 3:
                self._restart_lecture_browser()
            else:
                self._open_lecture(
                    self.active_lecture_url or "", self.active_lecture_id, force=True
                )

        def failed(message: str):
            self.lecture_health_check_running = False
            log.warning("lecture_health_worker_failed message=%s", message)

        self._run(
            self.browser.lecture_state,
            checked,
            "Проверяем вкладку лекции…",
            failed=failed,
        )

    def _soft_recovery_due(self) -> bool:
        now = time.monotonic()
        if self.soft_recovery_at is not None and now - self.soft_recovery_at < (
            SOFT_RECOVERY_EVERY_SECONDS
        ):
            return False
        self.soft_recovery_at = now
        # The reloaded room gets the same 30 seconds as a new one before its
        # captures count as stale again.
        self.last_capture_at = now
        return True

    def _enter_lecture_room(self):
        """Press the platform's entry control, with the student's name if asked for."""
        if self.entering_lecture_room:
            return
        self.entering_lecture_room = True
        name = self.student_name.text().strip()

        def entered(result: str):
            self.entering_lecture_room = False
            log.info("lecture_join_result result=%s", result)
            if result == "joined":
                self.statusBar().showMessage("Подключились к комнате лекции", 5000)

        def failed(message: str):
            self.entering_lecture_room = False
            log.warning("lecture_join_failed message=%s", message)

        self._run(
            lambda: self.browser.join_lecture(name),
            entered,
            "Подключаемся к комнате…",
            failed=failed,
        )

    def _room_ended(self, lesson):
        """A room that closes long before the pair ends is not the end of the pair.

        It was the wrong room (a past webinar of the subject) or the teacher closed
        it and opened another. The cached link used to keep the app on that dead
        room, with a blank tab, for the rest of the pair; now the room is dropped
        and the СДО is searched again.
        """
        lesson_id = self.active_lecture_id
        now = datetime.now().astimezone()
        # The pair is left only after its scheduled end: a room closed minutes in
        # is followed by a new one, and being out of it means being absent.
        early = lesson is not None and now < lesson.end_at + LEAVE_AFTER_END
        ignored = lesson is not None and self.db.get_rule(lesson.subject_name) is RuleMode.IGNORE
        if not early or not lesson_id or ignored or self._superseded(lesson, now):
            self._finish_active_lecture("room_ended")
            return
        minutes_left = max(0, int((lesson.end_at - now).total_seconds() // 60))
        log.warning(
            "lecture_room_ended_early lesson_id=%s minutes_left=%s", lesson_id, minutes_left
        )
        dead_room = self.active_lecture_url or self.db.get_resolved_link(lesson_id)
        first_time = (lesson_id, dead_room or "") not in self.room_lost_notified
        self.room_lost_notified.add((lesson_id, dead_room or ""))
        for url in {self.active_lecture_url, self.db.get_resolved_link(lesson_id)}:
            if url:
                self._reject_room(lesson_id, url)
        self.db.forget_resolved_link(lesson_id)
        # The tab stays: the next room opens in it instead of in a new one.
        self._finish_active_lecture("room_ended_early", close_tab=False)
        self.joined_lessons.discard(lesson_id)
        self.prompted_lessons.discard(lesson_id)
        self.lookup_not_before.pop(lesson_id, None)
        self.room_lost_lessons.add(lesson_id)
        self._fill_schedule()
        if not first_time:
            # The same dead room probed again: no second notification.
            self._resolve_from_sources(lesson)
            return
        self._background_problem(
            "Комната закрылась раньше конца пары",
            f"«{lesson.subject_name}»: до конца пары {minutes_left} мин — "
            "ищем новую комнату в СДО каждую минуту",
        )
        self._resolve_from_sources(lesson)

    def _rejected_rooms(self, lesson_id: str) -> frozenset[str]:
        now = time.time()
        rooms = self.db.get_setting("rejected_rooms", {}).get(lesson_id, {})
        return frozenset(
            url for url, rejected_at in rooms.items() if now - rejected_at < REJECTED_ROOM_SECONDS
        )

    def _reject_room(self, lesson_id: str, url: str):
        now = time.time()
        rejected = self.db.get_setting("rejected_rooms", {})
        rejected.setdefault(lesson_id, {})[url] = now
        # Keep only the last day: the lesson ids of older pairs will not come back.
        rejected = {
            lesson: rooms
            for lesson, rooms in rejected.items()
            if any(now - stamp < 86_400 for stamp in rooms.values())
        }
        self.db.set_setting("rejected_rooms", rejected)

    def _finish_active_lecture(self, reason: str, *, close_tab: bool = True):
        lesson_id = self.active_lecture_id
        log.info("lecture_monitoring_finished lesson_id=%s reason=%s", lesson_id, reason)
        self._clear_pending_for_lesson(lesson_id)
        self.active_lecture_id = None
        self.active_lecture_url = None
        self.db.set_setting("active_lecture", {})
        self.lecture_recovery_failures = 0
        if self.scan_timer.isActive():
            self.toggle_scanner()
        self.active_lesson = None
        if close_tab and self.close_tab_after.isChecked() and self.browser.probably_running:
            self._run(
                self.browser.close_lecture_tab,
                lambda closed: log.info("lecture_tab_close_result closed=%s", closed),
                "Закрываем вкладку закончившейся лекции…",
                failed=lambda message: log.warning("lecture_tab_close_failed message=%s", message),
            )

    def _restart_lecture_browser(self):
        url = self.active_lecture_url or ""
        lesson_id = self.active_lecture_id
        if not url or not lesson_id or self.opening_lecture_id:
            return
        self.opening_lecture_id = lesson_id
        muted = self.mute_lecture.isChecked()
        compact = self.compact_window.isChecked()
        self._run(
            lambda: self.browser.restart(
                url,
                muted=muted,
                width=520 if compact else 1100,
                height=360 if compact else 760,
            ),
            lambda browser_name: self._lecture_restarted(browser_name, lesson_id),
            "Перезапускаем браузер лекции…",
            failed=lambda message: self._lecture_open_failed(lesson_id, message),
        )

    def _lecture_restarted(self, browser_name: str, lesson_id: str | None):
        # A fresh browser starts the escalation over; otherwise every later
        # "lost" check restarted Chrome instead of first reloading the tab.
        self.lecture_recovery_failures = 0
        self._lecture_opened(browser_name, lesson_id)

    def _minimize_lecture(self, busy_text: str):
        self._run(
            self.browser.minimize,
            lambda _: None,
            busy_text,
            # Unattended background work must never pop up a modal error.
            failed=lambda message: log.warning("lecture_minimize_failed message=%s", message),
        )

    def _lecture_open_failed(self, lesson_id: str | None, message: str):
        if self.opening_lecture_id == lesson_id:
            self.opening_lecture_id = None
        log.warning("lecture_open_failed lesson_id=%s message=%s", lesson_id, message)
        self.statusBar().showMessage(
            "Лекцию пока не удалось открыть; повторим после обновления: " + message,
            6000,
        )

    def _fill_history(self):
        events = self.db.recent_qr_events()
        self.history_table.setRowCount(len(events))
        subjects = {lesson.external_id: lesson.subject_name for lesson in self.db.list_lessons()}
        if self.active_lesson is not None:
            subjects.setdefault(self.active_lesson.external_id, self.active_lesson.subject_name)
        for row, event in enumerate(events):
            time_item = QTableWidgetItem(format_relative_time(event.detected_at))
            time_item.setData(Qt.ItemDataRole.UserRole, event.detected_at.isoformat())
            time_item.setToolTip(f"{event.detected_at:%d.%m.%Y %H:%M:%S}")
            self.history_table.setItem(row, 0, time_item)
            label, token = HISTORY_STATUS.get(event.status, (event.status, "text"))
            status_item = QTableWidgetItem(label)
            status_item.setForeground(QBrush(QColor(self.colors[token])))
            status_item.setToolTip(f"Отпечаток QR: {event.token_hash[:16]}…")
            self.history_table.setItem(row, 1, status_item)
            subject = subjects.get(event.lesson_id or "", "—" if event.lesson_id else "Вручную")
            self.history_table.setItem(row, 2, QTableWidgetItem(subject))
            result_item = QTableWidgetItem(event.message or "—")
            result_item.setToolTip(event.message or "")
            self.history_table.setItem(row, 3, result_item)
        if events:
            latest = events[0]
            label, _colour = HISTORY_STATUS.get(latest.status, (latest.status, ""))
            self.last_scan_label.setText(format_relative_time(latest.detected_at))
            self.last_scan_detail.setText(f"Последний QR: {label.lower()}")
        else:
            self.last_scan_label.setText("QR ещё не находили")
            self.last_scan_detail.setText("Последнее сканирование появится здесь")

    def _refresh_relative_times(self):
        self._fill_history()
        self._update_now_card()

    def _update_now_card(self):
        """One line on what the app is doing: the pair now, or the next one."""
        now = datetime.now().astimezone()
        lessons = self.db.list_lessons()
        if self.active_lecture_id:
            lesson = self.active_lesson or next(
                (x for x in lessons if x.external_id == self.active_lecture_id), None
            )
            subject = lesson.subject_name if lesson else "лекция"
            until = f" · до {lesson.end_at:%H:%M}" if lesson else ""
            if self._attendance_already_marked(self.active_lecture_id):
                state = f'<span style="color:{self.colors["ok"]}">посещение отмечено ✓</span>'
            elif self.scan_timer.isActive():
                state = f'<span style="color:{self.colors["info"]}">ищем QR…</span>'
            else:
                state = "сканер выключен"
            text = f"<b>Идёт:</b> {subject}{until} · {state}"
            plain = (
                f"Идёт: {subject} · {state.split('>')[-2].split('<')[0] if '<' in state else state}"
            )
        else:
            upcoming = [x for x in lessons if x.end_at >= now]
            current = next((x for x in upcoming if x.start_at <= now), None)
            nearest = current or (upcoming[0] if upcoming else None)
            if nearest is not None and nearest.external_id in self.room_lost_lessons:
                text = (
                    f"<b>Сейчас:</b> {nearest.subject_name} · "
                    f'<span style="color:{self.colors["warn"]}">'
                    "комната закрылась — ищем новую ссылку в СДО</span>"
                )
                plain = f"{nearest.subject_name} · ищем новую комнату"
            elif nearest is None:
                text = plain = "Ближайших пар в расписании нет"
            else:
                mode = MODE_LABELS[self.db.get_rule(nearest.subject_name)]
                when = self._describe_start(nearest, now)
                text = (
                    f"<b>{'Сейчас' if nearest is current else 'Далее'}:</b> "
                    f"{nearest.subject_name} ({nearest.lesson_type}) · {when} · режим: {mode}"
                )
                plain = f"{nearest.subject_name} · {when}"
        self.now_card.setText(text)
        self.tray_status_action.setText(plain)
        self.tray.setToolTip(f"MIREA Lecture Assistant\n{plain}")

    @staticmethod
    def _describe_start(lesson, now: datetime) -> str:
        if lesson.start_at <= now:
            return f"идёт до {lesson.end_at:%H:%M}"
        minutes = int((lesson.start_at - now).total_seconds() // 60)
        if lesson.start_at.date() == now.date():
            if minutes < 60:
                return f"в {lesson.start_at:%H:%M}, через {minutes} мин"
            return f"сегодня в {lesson.start_at:%H:%M}"
        if lesson.start_at.date() == now.date() + timedelta(days=1):
            return f"завтра в {lesson.start_at:%H:%M}"
        return f"{lesson.start_at:%d.%m} в {lesson.start_at:%H:%M}"

    def toggle_scanner(self):
        if self.scan_timer.isActive():
            self.scan_timer.stop()
            self.scan_button.setText("Начать сканирование экрана")
            self.scan_state.setText("● Остановлен")
            self.scan_state.setStyleSheet(f"color: {self.colors['muted']}")
            self.pause_action.setText("Начать QR-сканирование")
            log.info("scanner_stopped")
            self._update_now_card()
        else:
            self.last_capture_at = time.monotonic()
            self.scan_timer.start(self.scan_interval.value() * 1000)
            self.scan_button.setText("Остановить сканирование")
            self.scan_state.setText("● Сканирует")
            self.scan_state.setStyleSheet(f"color: {self.colors['ok']}")
            self.pause_action.setText("Остановить QR-сканирование")
            log.info("scanner_started interval_seconds=%s", self.scan_interval.value())
            self._update_now_card()
            self._scan_tick()

    def _scan_tick(self):
        if self.scan_running:
            return
        self.scan_running = True
        self.scan_started_at = time.perf_counter()
        direct_capture = self.direct_capture.isChecked()
        worker = Worker(
            lambda: self._scan_source(direct_capture),
            "scan_frame",
            log_success=False,
        )
        self._start_worker(worker, self._scan_results, self._scan_failed, pool=self.scan_pool)

    def _scan_source(self, direct_capture: bool):
        if direct_capture:
            # Bound the whole browser operation, not only the screenshot request.
            png, page_text = run_async(self.browser.capture_page_state(), timeout=4.5)
            return self.scanner.decode_png(png), page_text
        return self.scanner.scan_once(), ""

    def _scan_results(self, observation):
        self.scan_running = False
        batch, page_text = observation
        now = time.monotonic()
        self.last_capture_at = now
        if now - self.last_capture_heartbeat >= 30:
            self.last_capture_heartbeat = now
            log.info(
                "scan_capture_ok direct=%s decoded=%s text_chars=%s elapsed_ms=%s",
                self.direct_capture.isChecked(),
                len(batch.decoded),
                len(page_text),
                round((time.perf_counter() - self.scan_started_at) * 1000),
            )
        if batch.decoded and not self._attendance_already_marked(self.active_lecture_id):
            # A frame captured before the success arrived must not submit again.
            for raw in batch.decoded:
                self._handle_qr(raw, silent_invalid=True)
        name = self.student_name.text().strip()
        group = self.group_edit.text().strip()
        own_message = f"{name} {group}".strip()
        if self.active_lecture_id and page_text:
            if self.chat_baseline is None:
                # What the chat already showed on joining is history, not a roll call.
                self.chat_baseline = chat_baseline(page_text, group, own_message)
            elif not self.qr_detected_in_lecture and classmates_report_attendance_issue(
                page_text, group, own_message, baseline=self.chat_baseline
            ):
                self._send_chat_fallback()
        self._continue_scan_without_gap()

    def _scan_failed(self, message: str):
        self.scan_running = False
        log.warning("scan_frame_failed message=%s", message)
        self.statusBar().showMessage(
            "Кадр не обработан, сканирование продолжается: " + message, 5000
        )
        self._continue_scan_without_gap()

    def _continue_scan_without_gap(self):
        if not self.scan_timer.isActive():
            return
        elapsed = time.perf_counter() - self.scan_started_at
        if elapsed >= self.scan_interval.value():
            QTimer.singleShot(0, self._scan_tick)

    def _manual_scan(self):
        raw = self.manual_qr.text().strip()
        if raw:
            self.manual_qr.clear()
            self._handle_qr(raw, allow_bare_token=True)

    def _handle_qr(self, raw: str, *, silent_invalid: bool = False, allow_bare_token: bool = False):
        qr, error = validate_qr(raw, allow_bare_token=allow_bare_token)
        if qr is None:
            if not silent_invalid:
                log.info("manual_qr_rejected reason=%s", error)
            if not silent_invalid:
                QMessageBox.information(self, "QR не принят", error)
            return
        self._mark_lecture_qr_seen()
        if self.deduplicator.is_duplicate(qr.token_hash):
            log.info("qr_duplicate fingerprint=%s", qr.token_hash[:12])
            self.statusBar().showMessage("Этот QR уже был обработан недавно", 4000)
            return
        lesson_id = self.active_lecture_id
        event_id = self.db.add_qr_event(qr.token_hash, "detected", lesson_id=lesson_id)
        log.info(
            "attendance_qr_detected event_id=%s lesson_id=%s fingerprint=%s",
            event_id,
            self.active_lecture_id,
            qr.token_hash[:12],
        )
        self.pending_qr[event_id] = PendingAttendance(
            raw_data=qr.raw_data,
            lesson_id=lesson_id,
            lecture_url=self.active_lecture_url,
            detected_at=datetime.now().astimezone(),
        )
        previous_event_id = self.latest_qr_event_by_lesson.get(lesson_id)
        self.latest_qr_event_by_lesson[lesson_id] = event_id
        if previous_event_id is not None and previous_event_id not in self.attendance_inflight:
            self.pending_qr.pop(previous_event_id, None)
            self.retry_attempts.pop(previous_event_id, None)
            self.db.update_qr_event(previous_event_id, "failed", "QR сменился до подтверждения")
            log.info(
                "attendance_qr_superseded old_event_id=%s new_event_id=%s lesson_id=%s",
                previous_event_id,
                event_id,
                lesson_id,
            )
        self._fill_history()
        if self.minimize_after_qr.isChecked() and self.browser.probably_running:
            self._minimize_lecture("QR найден, сворачиваем лекцию…")
        self.statusBar().showMessage("Найден QR посещаемости, отправляем отметку…", 6000)
        if not self.mirea.session:
            self.db.update_qr_event(event_id, "retrying", "Ожидается вход в MIREA")
            self._fill_history()
            self.tray.showMessage(
                "Не удалось отправить посещаемость",
                "Войдите в MIREA и отсканируйте QR ещё раз.",
                QSystemTrayIcon.MessageIcon.Warning,
                8000,
            )
            self._schedule_retry(event_id)
            return
        self._submit_attendance(event_id)

    def _submit_attendance(self, event_id: int):
        if event_id in self.attendance_inflight:
            return
        pending = self.pending_qr.get(event_id)
        if pending is None:
            return
        if self.latest_qr_event_by_lesson.get(pending.lesson_id) != event_id:
            self.pending_qr.pop(event_id, None)
            return
        if not self.mirea.session:
            self.db.update_qr_event(event_id, "retrying", "Ожидается вход в MIREA")
            self._schedule_retry(event_id)
            return
        self.db.update_qr_event(event_id, "detected", "Отправка в MIREA…")
        self._fill_history()
        self.attendance_inflight.add(event_id)
        log.info(
            "attendance_submit event_id=%s previous_failures=%s",
            event_id,
            self.retry_attempts.get(event_id, 0),
        )
        lesson_id = pending.lesson_id
        self._run(
            lambda: run_async(self.mirea.mark_attendance(pending.raw_data)),
            lambda result: self._attendance_finished(event_id, result, lesson_id),
            "Отправляем отметку…",
            failed=lambda message: self._attendance_exception(event_id, message),
            pool=self.scan_pool,
        )

    def _attendance_finished(self, event_id: int, result, lesson_id=...):
        self.attendance_inflight.discard(event_id)
        pending = self.pending_qr.get(event_id)
        if lesson_id is ...:
            lesson_id = pending.lesson_id if pending else None
        if result.success:
            log.info("attendance_success event_id=%s", event_id)
            self.db.update_qr_event(event_id, "submitted", result.message)
            # The lesson comes from the submission itself: a pending entry cleared
            # by an earlier success used to turn this into "no lesson" and wipe
            # the state of whatever was scanned without one.
            self._clear_pending_for_lesson(lesson_id)
            self._fill_history()
            self._persist_session()
            self.statusBar().showMessage(f"Посещение отмечено: {result.message}", 8000)
            self._stop_scanning_marked_lesson(lesson_id)
        elif pending is None:
            # Cleared meanwhile (another code of this lesson was accepted, or the
            # lecture ended); nothing to retry and nothing to count.
            final = (
                "Посещение уже отмечено другим QR"
                if self._attendance_already_marked(lesson_id)
                else result.message
            )
            self.db.update_qr_event(event_id, "failed", final)
            self._fill_history()
        else:
            if pending and self.latest_qr_event_by_lesson.get(pending.lesson_id) != event_id:
                self.pending_qr.pop(event_id, None)
                self.retry_attempts.pop(event_id, None)
                self.db.update_qr_event(event_id, "failed", "QR сменился до подтверждения")
                self._fill_history()
                return
            log.warning("attendance_rejected event_id=%s message=%s", event_id, result.message)
            if attendance_failure_counts_for_chat(result.message):
                # A real rejection of this token: resending it every five seconds
                # only raced the counter to the public chat message. The next
                # rotating code is submitted instead.
                # "rejected" keeps it deduplicated: the same code still on screen
                # was submitted again with every frame and counted as a new failure.
                self.db.update_qr_event(event_id, "rejected", result.message)
                self._fill_history()
                self._record_attendance_failure(event_id)
                self.pending_qr.pop(event_id, None)
                self.submit_attempts.pop(event_id, None)
                if self.latest_qr_event_by_lesson.get(lesson_id) == event_id:
                    self.latest_qr_event_by_lesson.pop(lesson_id, None)
                return
            log.info("attendance_failure_not_counted_for_chat event_id=%s", event_id)
            self.db.update_qr_event(event_id, "retrying", result.message)
            self._fill_history()
            self._recover_expired_session("attendance_rejected")
            self._schedule_retry(event_id)

    def _attendance_exception(self, event_id: int, message: str):
        self._persist_session()
        self.attendance_inflight.discard(event_id)
        pending = self.pending_qr.get(event_id)
        if pending is None:
            self.db.update_qr_event(event_id, "failed", message or "Ошибка соединения")
            self._fill_history()
            return
        if self.latest_qr_event_by_lesson.get(pending.lesson_id) != event_id:
            self.pending_qr.pop(event_id, None)
            self.retry_attempts.pop(event_id, None)
            self.db.update_qr_event(event_id, "failed", "QR сменился до подтверждения")
            self._fill_history()
            return
        self.db.update_qr_event(event_id, "retrying", message or "Ошибка соединения")
        self._fill_history()
        log.warning("attendance_exception event_id=%s message=%s", event_id, message)
        # A local/network exception is not a rejection from MIREA and must not
        # trigger the public fallback message in the lecture chat.
        self._recover_expired_session("attendance_exception")
        self._schedule_retry(event_id)

    def _schedule_retry(self, event_id: int):
        # One timer per event: a login finishing while a retry was already
        # scheduled used to start a second chain resubmitting the same code.
        if event_id in self.retry_scheduled:
            return
        pending = self.pending_qr.get(event_id)
        if pending and datetime.now().astimezone() - pending.detected_at > ATTENDANCE_RETRY_WINDOW:
            log.info("attendance_retry_abandoned event_id=%s reason=stale", event_id)
            self.pending_qr.pop(event_id, None)
            self.submit_attempts.pop(event_id, None)
            self.db.update_qr_event(event_id, "failed", "QR устарел, ждём следующий")
            self._fill_history()
            return
        # Every failure backs off, not only the ones counted for the chat message.
        attempts = self.submit_attempts.get(event_id, 0) + 1
        self.submit_attempts[event_id] = attempts
        delay_ms = 5_000 if attempts <= 5 else 30_000 if attempts <= 7 else 120_000
        self.retry_scheduled.add(event_id)
        QTimer.singleShot(delay_ms, lambda: self._retry_attendance(event_id))

    def _retry_attendance(self, event_id: int):
        self.retry_scheduled.discard(event_id)
        pending = self.pending_qr.get(event_id)
        if pending is None or self.latest_qr_event_by_lesson.get(pending.lesson_id) != event_id:
            return
        self._submit_attendance(event_id)

    def _record_attendance_failure(self, event_id: int):
        event_failures = self.retry_attempts.get(event_id, 0) + 1
        self.retry_attempts[event_id] = event_failures
        pending = self.pending_qr.get(event_id)
        lesson_id = pending.lesson_id if pending else None
        lesson_failures = self.attendance_failures_by_lesson.get(lesson_id, 0) + 1
        self.attendance_failures_by_lesson[lesson_id] = lesson_failures
        first_failure = self.attendance_first_failure_at.setdefault(lesson_id, time.monotonic())
        log.info(
            "attendance_failure_count event_id=%s event_failures=%s lesson_failures=%s",
            event_id,
            event_failures,
            lesson_failures,
        )
        # Rotating codes fail five times in twenty seconds; a real problem lasts.
        lasting = time.monotonic() - first_failure >= ATTENDANCE_FAILURE_SPAN_SECONDS
        if lesson_failures >= 5 and lasting:
            self._send_chat_fallback("five_attendance_failures", lesson_id)

    def _clear_pending_for_lesson(self, lesson_id: str | None):
        for pending_event_id, pending in tuple(self.pending_qr.items()):
            if pending.lesson_id == lesson_id:
                self.pending_qr.pop(pending_event_id, None)
                self.retry_attempts.pop(pending_event_id, None)
                self.submit_attempts.pop(pending_event_id, None)
        self.latest_qr_event_by_lesson.pop(lesson_id, None)
        self.attendance_failures_by_lesson.pop(lesson_id, None)
        self.attendance_first_failure_at.pop(lesson_id, None)

    @staticmethod
    def _session_fingerprint(session: dict) -> int:
        return hash(tuple(sorted((str(key), str(value)) for key, value in session.items())))

    def _persist_session(self):
        """pymirea refreshes tokens in place; keep them for the next start."""
        session = dict(self.mirea.session)
        fingerprint = self._session_fingerprint(session)
        if not session or fingerprint == self.persisted_session:
            return
        try:
            self.session_store.save(session)
        except Exception:
            log.warning("session_persist_failed", exc_info=True)
            return
        self.persisted_session = fingerprint
        log.info("session_persisted")

    def _discard_expired_session(self):
        self.mirea.session = {}
        self.pending_pulse_login = None
        self.session_obtained_at = None
        self.persisted_session = self._session_fingerprint({})
        try:
            self.session_store.clear()
        except Exception:
            log.warning("expired_session_discard_failed", exc_info=True)

    def _attendance_already_marked(self, lesson_id: str | None) -> bool:
        """Attendance is recorded once per lesson; scanning after that is pointless."""
        return bool(lesson_id and lesson_id in self.db.get_setting("marked_lessons", []))

    def _stop_scanning_marked_lesson(self, lesson_id: str | None):
        if lesson_id:
            marked = list(self.db.get_setting("marked_lessons", []))
            if lesson_id not in marked:
                marked.append(lesson_id)
                self.db.set_setting("marked_lessons", marked[-100:])
        if lesson_id == self.active_lecture_id and self.scan_timer.isActive():
            self.toggle_scanner()
            log.info("scanner_stopped reason=attendance_marked lesson_id=%s", lesson_id)

    def _mark_lecture_qr_seen(self):
        self.qr_detected_in_lecture = True
        if not self.active_lecture_id:
            return
        qr_seen_lessons = list(self.db.get_setting("qr_seen_lessons", []))
        if self.active_lecture_id not in qr_seen_lessons:
            qr_seen_lessons.append(self.active_lecture_id)
            self.db.set_setting("qr_seen_lessons", qr_seen_lessons[-100:])

    def _send_chat_fallback(self, reason: str = "classmate_messages", lesson_id: str | None = None):
        lesson_id = lesson_id or self.active_lecture_id
        if not lesson_id or lesson_id != self.active_lecture_id:
            log.warning(
                "chat_fallback_skipped reason=%s cause=lesson_changed lesson_id=%s active=%s",
                reason,
                lesson_id,
                self.active_lecture_id,
            )
            return
        if self.unreadable_chat_sent or not self.chat_fallback.isChecked():
            return
        name = self.student_name.text().strip()
        group = self.group_edit.text().strip()
        if not name or not group:
            if not self.chat_config_warned:
                self.chat_config_warned = True
                self.tray.showMessage(
                    "Нужно резервное сообщение",
                    "Заполните фамилию, имя и группу в настройках для сообщения преподавателю.",
                    QSystemTrayIcon.MessageIcon.Warning,
                    8000,
                )
            return
        if not self.browser.probably_running:
            log.warning("chat_fallback_skipped reason=%s cause=browser_unavailable", reason)
            return
        attempts, last = self.chat_attempts.get(lesson_id, (0, 0.0))
        if attempts >= CHAT_MAX_ATTEMPTS or (
            attempts and time.monotonic() - last < CHAT_RETRY_SECONDS
        ):
            return  # a failed send was retried with every frame, each with a balloon
        self.chat_attempts[lesson_id] = (attempts + 1, time.monotonic())
        self.unreadable_chat_sent = True
        log.info("chat_fallback_triggered reason=%s lesson_id=%s", reason, lesson_id)
        message = f"{name} {group}"
        self._run(
            lambda: self.browser.send_chat_message(message),
            lambda _: self._chat_sent(lesson_id),
            "Отправляем резервное сообщение в чат…",
            failed=lambda message: self._chat_failed(lesson_id, message),
        )

    def _chat_sent(self, lesson_id: str):
        log.info("chat_fallback_sent lesson_id=%s", lesson_id)
        if lesson_id:
            sent_lessons = list(self.db.get_setting("chat_sent_lessons", []))
            if lesson_id not in sent_lessons:
                sent_lessons.append(lesson_id)
                self.db.set_setting("chat_sent_lessons", sent_lessons[-100:])
        self.statusBar().showMessage("Сообщение преподавателю отправлено", 5000)

    def _chat_failed(self, lesson_id: str, message: str):
        if "подтвердить доставку" in message.casefold():
            # Enter was pressed, so resending may duplicate the public message.
            self._chat_sent(lesson_id)
            log.warning("chat_delivery_uncertain lesson_id=%s", lesson_id)
            self.statusBar().showMessage("Сообщение отправлено, подтверждение не получено", 6000)
            return
        if lesson_id == self.active_lecture_id:
            self.unreadable_chat_sent = False
        log.warning("chat_fallback_failed lesson_id=%s message=%s", lesson_id, message)
        self.statusBar().showMessage("Не удалось отправить сообщение в чат", 5000)
        attempts, _last = self.chat_attempts.get(lesson_id, (0, 0.0))
        if attempts >= CHAT_MAX_ATTEMPTS:
            self.tray.showMessage(
                "Чат MTS Link недоступен",
                f"{message}\n\nСообщение преподавателю не отправлено — напишите его сами.",
                QSystemTrayIcon.MessageIcon.Warning,
                8000,
            )

    def closeEvent(self, event):
        if self.force_exit or not QSystemTrayIcon.isSystemTrayAvailable():
            event.accept()
        else:
            event.ignore()
            self.hide()
            if not self.db.get_setting("tray_hint_shown", False):
                self.db.set_setting("tray_hint_shown", True)
                self.tray.showMessage(
                    "MIREA Lecture Assistant",
                    "Приложение продолжает работать в фоне. Открыть — щелчок по значку.",
                    QSystemTrayIcon.MessageIcon.Information,
                    4000,
                )

    def _restore(self):
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def _check_show_request(self):
        """A second launch asks for this, the running copy: its window, or its place."""
        from .app import version_tuple

        try:
            if not self.show_request.exists():
                return
            requested = self.show_request.read_text(encoding="ascii").strip()
            newer = version_tuple(requested) > version_tuple(__version__)
            self.show_response.write_text("handover" if newer else "shown", encoding="ascii")
            self.show_request.unlink()
        except (OSError, UnicodeDecodeError):
            return
        if newer:
            log.info("handing_over_to_newer_version version=%s", requested)
            self._hand_over()
            return
        log.info("window_shown_for_second_launch")
        self._restore()

    def _hand_over(self):
        """A newer version was started: quit and leave it the browser and the lecture."""
        self._persist_session()
        self.code_bridge.stop()
        self.mcp_access.stop()
        if self.code_watcher is not None:
            self.code_watcher.stop()
        self.force_exit = True
        self.tray.hide()
        QApplication.quit()

    def _quit(self):
        log.info("quit_requested")
        self._persist_session()
        self.code_bridge.stop()
        self.mcp_access.stop()
        if self.code_watcher is not None:
            self.code_watcher.stop()
        self.force_exit = True
        self.tray.hide()
        try:
            self.browser.close()
        except Exception:
            log.warning("browser_close_on_quit_failed", exc_info=True)
        QApplication.quit()
