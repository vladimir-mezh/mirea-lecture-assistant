from __future__ import annotations

import logging
import os
import secrets
import time
from datetime import UTC, datetime, timedelta
from functools import partial

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
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
    QSpinBox,
    QStackedWidget,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .async_runtime import run_async
from .browser_service import BrowserService, NotSignedInError
from .chat_detection import classmates_report_attendance_issue
from .database import Database
from .domain import PendingAttendance, RuleMode, SessionState
from .email_otp import EMAIL_PROVIDERS, EmailAccount, ImapOtpReader
from .mirea_service import MireaService
from .moodle import discover_course_urls, resolve_lecture_url
from .moodle_login import SignInFailed, sign_in
from .qr import QrDeduplicator, ScreenScanner, validate_qr
from .relative_time import format_relative_time
from .reliability import (
    attendance_failure_counts_for_chat,
)
from .security import SessionStore

log = logging.getLogger(__name__)


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
        "auto": "Gmail, Яндекс, Mail.ru и Outlook определяются по адресу. Для другой почты выберите ручной IMAP.",
        "gmail": 'Нужен <a href="https://myaccount.google.com/apppasswords">пароль приложения Google</a>.',
        "yandex": 'Включите IMAP и создайте <a href="https://id.yandex.ru/security/app-passwords">пароль приложения для Почты</a>. Вводить нужно сам пароль, не его название. Если адрес — алиас, укажите основной логин Яндекс ID в поле «Логин IMAP».',
        "mailru": 'Создайте <a href="https://help.mail.ru/mail/login/mailer/">пароль для внешнего приложения Mail.ru</a>.',
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


STYLE = """
QMainWindow, QWidget { background: #f5f7fb; color: #172033; font-family: 'Segoe UI'; font-size: 14px; }
QLabel { background: transparent; }
#sidebar { background: #172554; min-width: 210px; max-width: 210px; }
#brand { color: white; font-size: 19px; font-weight: 700; padding: 20px 14px; }
#nav { text-align: left; color: #cbd5e1; border: 0; border-radius: 8px; padding: 11px 16px; margin: 2px 10px; background: transparent; }
#nav:hover { background: #24346b; color: white; }
#nav:checked { background: #3451d1; color: white; font-weight: 600; }
#pageTitle { font-size: 27px; font-weight: 700; color: #111827; }
#muted { color: #64748b; }
#card { background: white; border: 1px solid #e4e9f2; border-radius: 12px; padding: 14px; }
QPushButton { background: #3451d1; color: white; border: 0; border-radius: 7px; padding: 9px 15px; font-weight: 600; }
QPushButton:hover { background: #2943b3; }
QPushButton:disabled { background: #aeb8d9; }
QPushButton#secondary { background: #e8edff; color: #2943b3; }
QLineEdit, QComboBox, QSpinBox { background: white; border: 1px solid #d8deea; border-radius: 7px; padding: 8px; }
QTableWidget { background: white; border: 1px solid #e4e9f2; border-radius: 10px; gridline-color: #eef1f6; }
QHeaderView::section { background: #f8fafc; border: 0; border-bottom: 1px solid #e4e9f2; padding: 9px; font-weight: 600; }
"""


class WorkerSignals(QObject):
    done = Signal(object)
    failed = Signal(str)


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
    def __init__(self, database: Database):
        super().__init__()
        self.db = database
        self.session_store = SessionStore()
        self.otp_reader = ImapOtpReader()
        try:
            session = self.session_store.load()
        except Exception:
            log.exception("keyring load failed")
            session = None
        self.mirea = MireaService(session)
        self.deduplicator = QrDeduplicator(database)
        self.scanner = ScreenScanner()
        from .paths import data_dir

        self.browser = BrowserService(data_dir() / "browser-profile")
        self.pending_qr: dict[int, PendingAttendance] = {}
        self.session_store.discard_obsolete_pending_attendance()
        self.latest_qr_event_by_lesson: dict[str | None, int] = {}
        self.retry_attempts: dict[int, int] = {}
        self.attendance_failures_by_lesson: dict[str | None, int] = {}
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
        self.scan_running = False
        self.scan_started_at = 0.0
        self.last_capture_heartbeat = 0.0
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
        self.session_recheck_scheduled = False
        self.remember_login_requested = False
        self.login_started_at = datetime.now(UTC)

        self.setWindowTitle("MIREA Lecture Assistant")
        self.resize(1080, 700)
        self.setMinimumSize(880, 580)
        self.setStyleSheet(STYLE)
        self._build_ui()
        self._build_tray()
        self._load_settings()
        self.refresh_views()
        log.info("main_window_ready")

        self.clock = QTimer(self)
        self.clock.timeout.connect(self._refresh_relative_times)
        self.clock.start(30_000)
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
        self.lecture_health_check_running = False
        self.lecture_recovery_failures = 0
        QTimer.singleShot(0, self._startup_auth)

    def _build_ui(self):
        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        sidebar = QWidget(objectName="sidebar")
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(0, 0, 0, 16)
        brand = QLabel("MIREA\nLecture Assistant", objectName="brand")
        side.addWidget(brand)
        self.nav_buttons = []
        for index, text in enumerate(("Расписание", "QR-сканер", "История", "Настройки")):
            button = QPushButton(text, objectName="nav")
            button.setCheckable(True)
            button.clicked.connect(partial(self._show_page, index))
            side.addWidget(button)
            self.nav_buttons.append(button)
        side.addStretch()
        self.auth_status = QLabel("● MIREA: не выполнен вход")
        self.auth_status.setStyleSheet("color: #fbbf24; padding: 12px 18px;")
        self.auth_status.setWordWrap(True)
        side.addWidget(self.auth_status)
        root.addWidget(sidebar)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._schedule_page())
        self.pages.addWidget(self._scanner_page())
        self.pages.addWidget(self._history_page())
        self.pages.addWidget(self._settings_page())
        root.addWidget(self.pages, 1)
        self.setCentralWidget(central)
        self._show_page(0)

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
        refresh.clicked.connect(self.refresh_schedule)
        actions.addWidget(refresh)
        layout.addLayout(actions)
        rules = QHBoxLayout()
        rules.addWidget(QLabel("Автооткрытие предмета:"))
        self.subject_rule_subject = QComboBox()
        self.subject_rule_subject.setMinimumWidth(330)
        self.subject_rule_subject.currentTextChanged.connect(self._selected_subject_changed)
        rules.addWidget(self.subject_rule_subject, 1)
        self.subject_rule_mode = QComboBox()
        self.subject_rule_mode.addItems([mode.value for mode in RuleMode])
        self.subject_rule_mode.currentTextChanged.connect(self._selected_rule_changed)
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
                "Режим предмета",
                "Ссылка",
                "",
                "",
            )
        )
        self.schedule_table.verticalHeader().setVisible(False)
        self.schedule_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        header = self.schedule_table.horizontalHeader()
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
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
        self.scan_state.setStyleSheet("color: #64748b")
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
        self.history_table.setHorizontalHeaderLabels(
            ("Обнаружен", "Статус", "Отпечаток", "Результат")
        )
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.history_table, 1)
        return page

    def _settings_page(self):
        page, layout = self._page_shell(
            "Настройки", "Параметры сохраняются локально на этом компьютере"
        )
        card = QGroupBox(objectName="card")
        form = QFormLayout(card)
        self.group_edit = QLineEdit()
        self.group_edit.setPlaceholderText("Например, ИКБО-01-24")
        form.addRow("Группа", self.group_edit)
        self.join_before = QSpinBox()
        self.join_before.setRange(0, 30)
        self.join_before.setSuffix(" мин")
        form.addRow("Подготовиться до пары", self.join_before)
        self.scan_interval = QSpinBox()
        self.scan_interval.setRange(1, 5)
        self.scan_interval.setSuffix(" сек")
        form.addRow("Интервал сканирования", self.scan_interval)
        self.direct_capture = QCheckBox("Сканировать вкладку браузера напрямую")
        form.addRow("Источник изображения", self.direct_capture)
        self.compact_window = QCheckBox("Открывать лекцию в компактном окне 520 × 360")
        form.addRow("Размер окна", self.compact_window)
        self.hd_capture = QCheckBox("Захватывать кадр 1920 × 1080 независимо от размера окна")
        self.hd_capture.setToolTip(
            "Без этого QR в маленьком окне слишком мелкий, чтобы его распознать"
        )
        form.addRow("Качество захвата", self.hd_capture)
        self.mute_lecture = QCheckBox("Открывать лекцию без звука")
        form.addRow("Звук", self.mute_lecture)
        self.close_tab_after = QCheckBox("Закрывать вкладку лекции, когда пара закончилась")
        form.addRow("После пары", self.close_tab_after)
        self.minimize_on_open = QCheckBox("Сворачивать окно лекции сразу после открытия")
        self.minimize_on_open.setToolTip(
            "Захват идёт с самой вкладки, поэтому свёрнутое окно сканируется так же"
        )
        form.addRow("После открытия", self.minimize_on_open)
        self.minimize_after_qr = QCheckBox("Сворачивать окно лекции после распознавания QR")
        form.addRow("После QR", self.minimize_after_qr)
        self.chat_fallback = QCheckBox("Использовать резервное сообщение в чат")
        form.addRow("Резервный сценарий", self.chat_fallback)
        self.student_name = QLineEdit()
        self.student_name.setPlaceholderText("Иванов Иван")
        form.addRow("Фамилия и имя", self.student_name)
        self.auto_login = QCheckBox("Автоматически входить и получать код из почты")
        form.addRow("Вход", self.auto_login)
        self.email_provider = QComboBox()
        _fill_email_providers(self.email_provider)
        form.addRow("Провайдер почты", self.email_provider)
        self.email_address = QLineEdit()
        self.email_address.setPlaceholderText("example@mail.ru")
        form.addRow("Почта для кода", self.email_address)
        self.email_app_password = QLineEdit()
        self.email_app_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.email_app_password.setPlaceholderText("Пароль приложения почты")
        form.addRow("Пароль приложения", self.email_app_password)
        self.imap_username = QLineEdit()
        self.imap_username.setPlaceholderText("Обычно определяется по адресу")
        form.addRow("Логин IMAP", self.imap_username)
        self.imap_host = QLineEdit()
        self.imap_port = QSpinBox()
        self.imap_port.setRange(1, 65535)
        self.imap_port.setValue(993)
        form.addRow("IMAP-сервер", self.imap_host)
        form.addRow("Порт IMAP", self.imap_port)
        self.email_hint = QLabel()
        self.email_hint.setOpenExternalLinks(True)
        self.email_hint.setWordWrap(True)
        form.addRow("", self.email_hint)
        self.email_provider.currentIndexChanged.connect(
            lambda: _update_imap_fields(
                self.email_provider, self.imap_host, self.imap_port, self.email_hint
            )
        )
        _update_imap_fields(self.email_provider, self.imap_host, self.imap_port, self.email_hint)
        test_email = QPushButton("Проверить подключение к почте", objectName="secondary")
        test_email.clicked.connect(self._test_email_connection)
        form.addRow("", test_email)
        open_logs = QPushButton("Открыть папку журналов", objectName="secondary")
        open_logs.clicked.connect(self._open_logs_folder)
        form.addRow("Диагностика", open_logs)
        save = QPushButton("Сохранить")
        save.clicked.connect(self._save_settings)
        form.addRow("", save)
        layout.addWidget(card)
        layout.addStretch()
        return page

    def _build_tray(self):
        icon = QApplication.windowIcon()
        self.tray = QSystemTrayIcon(icon, self)
        menu = QMenu()
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
                self._restore() if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None
            )
        )
        self.tray.show()

    def _show_page(self, index: int):
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
        self._update_group_label()

    def _save_settings(self):
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
                    return
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
            return
        if self.student_name.text().strip() and self.group_edit.text().strip():
            self.chat_config_warned = False
        self.scan_timer.setInterval(self.scan_interval.value() * 1000)
        self._update_group_label()
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

    def _open_logs_folder(self):
        from .paths import data_dir

        log_dir = data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log.info("logs_folder_opened")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_dir)))

    def _update_group_label(self):
        group = self.db.get_setting("group", "")
        self.group_label.setText(f"Группа: {group}" if group else "Группа не указана")

    def _run(self, function, done, busy_text: str = "Выполняется…", failed=None):
        self.statusBar().showMessage(busy_text)
        operation = busy_text.casefold().replace("…", "").replace(" ", "_").replace(",", "")
        worker = Worker(function, operation)
        self._start_worker(worker, done, failed or self._operation_failed, clear_status=True)

    def _start_worker(self, worker, done, failed, *, clear_status: bool = False):
        """Run a worker, keeping it alive until its result has been delivered.

        QThreadPool deletes a finished QRunnable, which takes its signal object
        with it; a queued result can then be dropped before the main thread ever
        sees it, leaving the operation hanging with no error anywhere.
        """
        worker.setAutoDelete(False)
        self.active_workers.add(worker)

        def deliver(handler, value):
            self.active_workers.discard(worker)
            if clear_status:
                self.statusBar().clearMessage()
            handler(value)

        worker.signals.done.connect(lambda value: deliver(done, value))
        worker.signals.failed.connect(lambda message: deliver(failed, message))
        self.pool.start(worker)

    def _operation_failed(self, message: str):
        log.error("ui_operation_failed message=%s", message)
        self.statusBar().clearMessage()
        QMessageBox.warning(self, "Не удалось выполнить действие", message or "Неизвестная ошибка")

    def login(self):
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
        log.warning("automatic_login_failed message=%s", message)
        if self.automatic_login_cycle:
            self.statusBar().showMessage("Автовход временно недоступен; повторим попытку", 8000)
            self._schedule_login_retry(message)
        else:
            self._operation_failed(message)

    def _initial_login_finished(self, payload):
        self.login_in_progress = False
        result, self.email_uid_before_login = payload
        self._login_finished(result)

    def _login_finished(self, result):
        self.login_in_progress = False
        if result.challenge:
            if self.otp_submission_attempted:
                if self.automatic_login_cycle:
                    log.warning("automatic_2fa_rejected; further login requires manual action")
                    self.automatic_login_cycle = False
                    self._operation_failed(
                        result.message or "Код не принят; повторите вход вручную"
                    )
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
            if email_credentials:
                self._run(
                    lambda: self.otp_reader.wait_for_code(
                        email_credentials,
                        self.login_started_at,
                        after_uid=self.email_uid_before_login,
                    ),
                    lambda code: self._complete_2fa(result.challenge, code),
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
            # Never start another SSO flow after a rejected/failed code.
            # The account may be locked by repeated automatic challenges.
            self.pending_login_credentials = None
            self.pending_email_credentials = None
            self.login_in_progress = False
            diagnostic_id = secrets.token_hex(4)
            log.error("login_rejected id=%s message=%s", diagnostic_id, result.message)
            self._operation_failed(f"{result.message}\n\nКод диагностики: {diagnostic_id}")
            return
        try:
            self.session_store.save(self.mirea.session)
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
        self.email_uid_before_login = None
        self.otp_submission_attempted = False
        self.login_cycle_retries = 0
        self.login_retry_attempt = 0
        self.login_retry_scheduled = False
        self.automatic_login_cycle = False
        self.session_recheck_scheduled = False
        self.auth_status.setText("● MIREA: выполнен вход")
        log.info("login_success")
        self.auth_status.setStyleSheet("color: #86efac; padding: 12px 18px;")
        self.login_button.setText("Войти заново")
        self.statusBar().showMessage("Вход выполнен", 3000)
        self.refresh_schedule()
        for event_id in tuple(self.pending_qr):
            self._retry_attendance(event_id)

    def _complete_2fa(self, challenge, code: str):
        self.otp_submission_attempted = True
        self.login_in_progress = True
        self._run(
            lambda: run_async(self.mirea.complete_2fa(challenge, code)),
            self._login_finished,
            "Проверяем код…",
            lambda message: self._otp_wait_failed(challenge, message),
        )

    def _manual_2fa(self, challenge, reason: str = ""):
        prompt = "Не удалось получить код автоматически. Введите код из письма:"
        if reason:
            prompt = f"{prompt}\n\n{reason}"
        code, ok = QInputDialog.getText(self, "Двухфакторная авторизация", prompt)
        if ok and code.strip():
            self._complete_2fa(challenge, code.strip())
        else:
            self.login_in_progress = False

    def _otp_wait_failed(self, challenge, message: str):
        if self.automatic_login_cycle:
            log.warning("automatic_otp_failed message=%s", message)
            self.login_in_progress = False
            self.automatic_login_cycle = False
            self._operation_failed(message)
            return
        self._manual_2fa(challenge, message)

    def _schedule_login_retry(self, reason: str | None = None):
        # Deliberately fail closed: automatic retries can request unlimited OTPs.
        self.login_retry_scheduled = False
        self.automatic_login_cycle = False
        log.warning("automatic_login_stopped reason=%s", reason or "unknown")
        self.statusBar().showMessage("Автовход остановлен. Повторите вход вручную.", 15000)

    def _run_scheduled_login(self):
        if not self.login_retry_scheduled:
            return
        self.login_retry_scheduled = False
        self._auto_login()

    def _startup_auth(self):
        if os.environ.get("MIREA_ASSISTANT_SMOKE_TEST") == "1":
            # A build smoke test must never reach MIREA and trigger a real 2FA email.
            log.info("startup_auth_skipped reason=smoke_test")
            return
        # A valid saved session must not generate a fresh OTP on every launch.
        if self.mirea.session:
            self._run(
                lambda: run_async(self.mirea.verify_state()),
                self._session_verified,
                "Проверяем сохранённый вход…",
                lambda _message: self._session_verified(SessionState.UNKNOWN),
            )
        else:
            self._auto_login()

    def _session_verified(self, state: SessionState):
        log.info("stored_session_verified state=%s", state.value)
        if state is SessionState.EXPIRED:
            self._auto_login()
            return
        if state is SessionState.UNKNOWN:
            self.statusBar().showMessage("MIREA пока недоступна; повторим проверку", 8000)
            if not self.session_recheck_scheduled:
                self.session_recheck_scheduled = True
                QTimer.singleShot(60_000, self._retry_session_verification)
            return
        self.auth_status.setText("● MIREA: выполнен вход")
        self.auth_status.setStyleSheet("color: #86efac; padding: 12px 18px;")
        self.login_button.setText("Войти заново")
        self.statusBar().showMessage("Сохранённый вход восстановлен", 3000)
        self.refresh_schedule()
        for event_id in tuple(self.pending_qr):
            self._retry_attendance(event_id)

    def _retry_session_verification(self):
        if not self.session_recheck_scheduled:
            return
        self.session_recheck_scheduled = False
        if self.login_in_progress or not self.mirea.session:
            return
        self._run(
            lambda: run_async(self.mirea.verify_state()),
            self._session_verified,
            "Повторно проверяем MIREA…",
            lambda _message: self._session_verified(SessionState.UNKNOWN),
        )

    def _auto_login(self):
        if self.login_in_progress:
            return
        if not bool(self.db.get_setting("auto_login", True)):
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

    def _schedule_refresh_failed(self, message: str):
        self.schedule_refresh_running = False
        log.warning("schedule_refresh_failed message=%s", message)
        self.statusBar().showMessage(
            "Расписание пока недоступно; повторим проверку через минуту: " + message,
            6000,
        )
        self._recover_expired_session("schedule_refresh")

    def _recover_expired_session(self, reason: str):
        """Re-enter automatically when a saved session expires while the app is running."""
        if self.login_in_progress or self.auth_recovery_running or not self.mirea.session:
            return
        self.auth_recovery_running = True
        log.info("session_recovery_check reason=%s", reason)

        def checked(state: SessionState):
            self.auth_recovery_running = False
            if state is SessionState.VALID:
                return
            if state is SessionState.UNKNOWN:
                log.info("session_recovery_deferred reason=network")
                return
            log.warning("session_expired reason=%s", reason)
            self.mirea.session = {}
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
        self._evaluate_current_lessons(lessons)

    def _evaluate_current_lessons(self, lessons):
        now = datetime.now().astimezone()
        lead_minutes = self.join_before.value()
        # Teachers may keep a room live after the nominal bell. Prefer the newest
        # eligible lesson so an overrun never wins over a pair that has just begun.
        for lesson in sorted(lessons, key=lambda item: item.start_at, reverse=True):
            if not (
                lesson.start_at - timedelta(minutes=lead_minutes)
                <= now
                <= lesson.end_at + timedelta(minutes=90)
            ):
                continue
            if lesson.external_id in self.joined_lessons:
                continue
            mode = self.db.get_rule(lesson.subject_name)
            if mode is RuleMode.IGNORE:
                continue
            url = (
                self.db.get_resolved_link(lesson.external_id)
                or lesson.source_url
                or self.db.get_link(lesson.subject_name)
            )
            if not url:
                # The webinar is often created after the pair has begun, so this
                # runs again on every refresh until the room shows up.
                self._resolve_from_sources(lesson)
                continue
            if mode is RuleMode.AUTO:
                log.info(
                    "current_lesson_action lesson_id=%s mode=AUTO action=open", lesson.external_id
                )
                self._open_lecture(url, lesson.external_id)
                break
            if lesson.external_id in self.prompted_lessons:
                continue
            self.prompted_lessons.add(lesson.external_id)
            log.info(
                "current_lesson_action lesson_id=%s mode=ASK action=prompt", lesson.external_id
            )
            answer = QMessageBox.question(
                self,
                "Лекция доступна",
                f"Появилась лекция «{lesson.subject_name}». Открыть её?",
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._open_lecture(url, lesson.external_id)
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
            return self.browser.read_html(url)

    def _sign_in_to_sdo(self) -> bool:
        """Sign in to the СДО with the stored MIREA account and the emailed code."""
        try:
            credentials = self.session_store.load_credentials()
            email_credentials = self.session_store.load_email_credentials()
        except Exception as exc:  # noqa: BLE001 - keyring backends fail in many ways
            log.warning("sdo_sign_in_no_credentials error=%s", exc)
            return False
        if not credentials:
            log.info("sdo_sign_in_skipped reason=no_credentials")
            return False
        if not self.db.reserve_auth_attempt("sdo"):
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
            return self.otp_reader.wait_for_code(
                email_credentials, requested_at, after_uid=latest_uid
            )

        username, password = credentials
        log.info("sdo_sign_in_started")
        self.browser.run_on_new_page(
            lambda page: sign_in(
                page, username=username, password=password, request_code=request_code
            )
        )
        return True

    def _resolve_from_sources(self, lesson):
        if lesson.external_id in self.resolving_lessons:
            return
        group = self.group_edit.text().strip()
        saved = self.db.get_sources(lesson.subject_name)
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
            )
            # Automatically discovered courses are refreshed each pass. Do not
            # mix them permanently with explicitly configured manual sources.
            return webinar, []

        self._run(
            lookup,
            lambda payload: self._source_resolved(lesson, *payload),
            "Ищем вебинар в СДО…",
            failed=lambda message: self._source_lookup_failed(lesson, message),
        )

    def _source_resolved(self, lesson, webinar, discovered: list[str]):
        self.resolving_lessons.discard(lesson.external_id)
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
        self.db.set_resolved_link(lesson.external_id, webinar.join_url)
        log.info("webinar_resolved lesson_id=%s", lesson.external_id)
        self._fill_schedule()
        self.statusBar().showMessage(f"Вебинар найден: {lesson.subject_name}", 5000)
        mode = self.db.get_rule(lesson.subject_name)
        if mode is RuleMode.AUTO:
            self._open_lecture(webinar.join_url, lesson.external_id)
        elif mode is RuleMode.ASK and lesson.external_id not in self.prompted_lessons:
            self.prompted_lessons.add(lesson.external_id)
            answer = QMessageBox.question(
                self,
                "Вебинар найден",
                f"В СДО появился вебинар «{lesson.subject_name}». Открыть?",
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._open_lecture(webinar.join_url, lesson.external_id)

    def _source_lookup_failed(self, lesson, message: str):
        self.resolving_lessons.discard(lesson.external_id)
        log.warning("webinar_lookup_failed lesson_id=%s message=%s", lesson.external_id, message)
        self.statusBar().showMessage("Вебинар в СДО пока не найден: " + message, 6000)

    def refresh_views(self):
        self._fill_schedule()
        self._fill_history()

    def _fill_schedule(self):
        focused = QApplication.focusWidget()
        if (
            self.isActiveWindow()
            and focused is not None
            and self.schedule_table.isAncestorOf(focused)
        ):
            # The minute tick rebuilds every cell widget. Doing that while a link is
            # being typed destroys the editor before editingFinished can save it.
            log.debug("schedule_redraw_deferred reason=cell_editing")
            QTimer.singleShot(3_000, self._fill_schedule)
            return
        lessons = self.db.list_lessons()
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
            for column, value in enumerate(values):
                self.schedule_table.setItem(row, column, QTableWidgetItem(value))
            mode_item = QTableWidgetItem(self.db.get_rule(lesson.subject_name).value)
            mode_item.setToolTip("Общее правило предмета; изменяется над таблицей")
            mode_item.setFlags(mode_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.schedule_table.setItem(row, 5, mode_item)
            found = self.db.get_resolved_link(lesson.external_id)
            link = QLineEdit(
                found or self.db.get_link(lesson.subject_name) or lesson.source_url or ""
            )
            if found:
                link.setToolTip("Адрес найден в СДО для этого занятия")
            link.setPlaceholderText("https://…")
            link.editingFinished.connect(
                lambda subject=lesson.subject_name, editor=link: self.db.set_link(
                    subject, editor.text().strip()
                )
            )
            self.schedule_table.setCellWidget(row, 6, link)
            open_button = QPushButton("Открыть", objectName="secondary")
            open_button.clicked.connect(
                lambda _=False, editor=link, lesson_id=lesson.external_id: self._open_lecture(
                    editor.text().strip(), lesson_id
                )
            )
            self.schedule_table.setCellWidget(row, 7, open_button)
            sources_button = QPushButton("Источник", objectName="secondary")
            sources_button.setToolTip("Страницы СДО, где искать вебинар этого предмета")
            sources_button.clicked.connect(
                lambda _=False, subject=lesson.subject_name: self._edit_sources(subject)
            )
            self.schedule_table.setCellWidget(row, 8, sources_button)

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
                mode_item.setText(mode.value)
        self.statusBar().showMessage(
            f"Для всех занятий «{subject}» установлен режим {mode.value}", 4000
        )

    def _selected_subject_changed(self, subject: str):
        if not subject:
            self.subject_rule_mode.setEnabled(False)
            return
        self.subject_rule_mode.setEnabled(True)
        self.subject_rule_mode.blockSignals(True)
        self.subject_rule_mode.setCurrentText(self.db.get_rule(subject).value)
        self.subject_rule_mode.blockSignals(False)

    def _selected_rule_changed(self, value: str):
        subject = self.subject_rule_subject.currentText()
        if subject and value:
            self._set_subject_rule(subject, RuleMode(value))

    def _open_lecture(self, url: str, lesson_id: str | None = None, *, force: bool = False):
        if not url:
            QMessageBox.information(
                self, "Ссылка не указана", "Сначала вставьте ссылку на лекцию в таблице."
            )
            return
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
            lambda browser_name: self._lecture_opened(browser_name, lesson_id),
            "Открываем лекцию…",
            failed=lambda message: self._lecture_open_failed(lesson_id, message),
        )

    def _lecture_opened(self, browser_name: str, lesson_id: str | None):
        self.opening_lecture_id = None
        if lesson_id:
            self.joined_lessons.add(lesson_id)
        self.active_lecture_url = self.browser.lecture_url
        log.info("lecture_opened lesson_id=%s browser=%s", lesson_id, browser_name)
        if lesson_id != self.active_lecture_id:
            self.active_lecture_id = lesson_id
            sent_lessons = self.db.get_setting("chat_sent_lessons", [])
            self.unreadable_chat_sent = bool(lesson_id and lesson_id in sent_lessons)
            qr_seen_lessons = self.db.get_setting("qr_seen_lessons", [])
            self.qr_detected_in_lecture = bool(lesson_id and lesson_id in qr_seen_lessons)
            self.chat_config_warned = False
        self.statusBar().showMessage(f"Лекция открыта в {browser_name}", 4000)
        if self.minimize_on_open.isChecked():
            self._run(self.browser.minimize, lambda _: None, "Сворачиваем окно лекции…")
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
        lesson = next(
            (x for x in self.db.list_lessons() if x.external_id == self.active_lecture_id), None
        )
        if lesson is None or datetime.now().astimezone() > lesson.end_at + timedelta(hours=2):
            self._finish_active_lecture("safety_timeout")
            return
        self.lecture_health_check_running = True

        def checked(state: str):
            self.lecture_health_check_running = False
            if state == "live":
                self.lecture_recovery_failures = 0
                return
            if state == "ended":
                self._finish_active_lecture("room_ended")
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

    def _finish_active_lecture(self, reason: str):
        lesson_id = self.active_lecture_id
        log.info("lecture_monitoring_finished lesson_id=%s reason=%s", lesson_id, reason)
        self._clear_pending_for_lesson(lesson_id)
        self.active_lecture_id = None
        self.active_lecture_url = None
        self.lecture_recovery_failures = 0
        if self.scan_timer.isActive():
            self.toggle_scanner()
        if self.close_tab_after.isChecked() and self.browser.is_running:
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
            lambda browser_name: self._lecture_opened(browser_name, lesson_id),
            "Перезапускаем браузер лекции…",
            failed=lambda message: self._lecture_open_failed(lesson_id, message),
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
        status_labels = {
            "detected": "Отправляется",
            "retrying": "Повторная отправка",
            "ignored": "Пропущен",
            "submitted": "Посещение подтверждено",
            "failed": "Ошибка",
            "invalid": "Неверный QR",
        }
        for row, event in enumerate(events):
            time_item = QTableWidgetItem(format_relative_time(event.detected_at))
            time_item.setData(Qt.ItemDataRole.UserRole, event.detected_at.isoformat())
            self.history_table.setItem(row, 0, time_item)
            self.history_table.setItem(
                row, 1, QTableWidgetItem(status_labels.get(event.status, event.status))
            )
            self.history_table.setItem(row, 2, QTableWidgetItem(event.token_hash[:12] + "…"))
            self.history_table.setItem(row, 3, QTableWidgetItem(event.message or "—"))
        if events:
            latest = events[0]
            self.last_scan_label.setText(format_relative_time(latest.detected_at))
            self.last_scan_detail.setText("Последний QR был отсканирован")
        else:
            self.last_scan_label.setText("QR ещё не находили")
            self.last_scan_detail.setText("Последнее сканирование появится здесь")

    def _refresh_relative_times(self):
        self._fill_history()

    def toggle_scanner(self):
        if self.scan_timer.isActive():
            self.scan_timer.stop()
            self.scan_button.setText("Начать сканирование экрана")
            self.scan_state.setText("● Остановлен")
            self.scan_state.setStyleSheet("color: #64748b")
            self.pause_action.setText("Начать QR-сканирование")
            log.info("scanner_stopped")
        else:
            self.scan_timer.start(self.scan_interval.value() * 1000)
            self.scan_button.setText("Остановить сканирование")
            self.scan_state.setText("● Сканирует")
            self.scan_state.setStyleSheet("color: #16a34a")
            self.pause_action.setText("Остановить QR-сканирование")
            log.info("scanner_started interval_seconds=%s", self.scan_interval.value())
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
        self._start_worker(worker, self._scan_results, self._scan_failed)

    def _scan_source(self, direct_capture: bool):
        if direct_capture and self.browser.is_running:
            png, page_text = run_async(self.browser.capture_page_state(), timeout=4.5)
            return self.scanner.decode_png(png), page_text
        return self.scanner.scan_once(), ""

    def _scan_results(self, observation):
        self.scan_running = False
        batch, page_text = observation
        now = time.monotonic()
        if now - self.last_capture_heartbeat >= 30:
            self.last_capture_heartbeat = now
            log.info(
                "scan_capture_ok direct=%s decoded=%s text_chars=%s elapsed_ms=%s",
                self.direct_capture.isChecked(),
                len(batch.decoded),
                len(page_text),
                round((time.perf_counter() - self.scan_started_at) * 1000),
            )
        if batch.decoded:
            for raw in batch.decoded:
                self._handle_qr(raw, silent_invalid=True)
        name = self.student_name.text().strip()
        group = self.group_edit.text().strip()
        own_message = f"{name} {group}".strip()
        if (
            self.active_lecture_id
            and not self.qr_detected_in_lecture
            and classmates_report_attendance_issue(page_text, group, own_message)
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
        if self.minimize_after_qr.isChecked() and self.browser.is_running:
            self._run(self.browser.minimize, lambda _: None, "QR найден, сворачиваем лекцию…")
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
        self._run(
            lambda: run_async(self.mirea.mark_attendance(pending.raw_data)),
            lambda result: self._attendance_finished(event_id, result),
            "Отправляем отметку…",
            failed=lambda message: self._attendance_exception(event_id, message),
        )

    def _attendance_finished(self, event_id: int, result):
        self.attendance_inflight.discard(event_id)
        pending = self.pending_qr.get(event_id)
        if result.success:
            log.info("attendance_success event_id=%s", event_id)
            self.db.update_qr_event(event_id, "submitted", result.message)
            self._clear_pending_for_lesson(pending.lesson_id if pending else None)
            self._fill_history()
            self.statusBar().showMessage(f"Посещение отмечено: {result.message}", 8000)
            self._stop_scanning_marked_lesson(pending.lesson_id if pending else None)
        else:
            if pending and self.latest_qr_event_by_lesson.get(pending.lesson_id) != event_id:
                self.pending_qr.pop(event_id, None)
                self.retry_attempts.pop(event_id, None)
                self.db.update_qr_event(event_id, "failed", "QR сменился до подтверждения")
                self._fill_history()
                return
            log.warning("attendance_rejected event_id=%s message=%s", event_id, result.message)
            self.db.update_qr_event(event_id, "retrying", result.message)
            self._fill_history()
            if attendance_failure_counts_for_chat(result.message):
                self._record_attendance_failure(event_id)
            else:
                log.info("attendance_failure_not_counted_for_chat event_id=%s", event_id)
            self._recover_expired_session("attendance_rejected")
            self._schedule_retry(event_id)

    def _attendance_exception(self, event_id: int, message: str):
        self.attendance_inflight.discard(event_id)
        pending = self.pending_qr.get(event_id)
        if pending and self.latest_qr_event_by_lesson.get(pending.lesson_id) != event_id:
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
        failures = self.retry_attempts.get(event_id, 0)
        delay_ms = 5_000 if failures < 5 else 30_000 if failures == 5 else 120_000
        QTimer.singleShot(delay_ms, lambda: self._retry_attendance(event_id))

    def _retry_attendance(self, event_id: int):
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
        log.info(
            "attendance_failure_count event_id=%s event_failures=%s lesson_failures=%s",
            event_id,
            event_failures,
            lesson_failures,
        )
        if lesson_failures >= 5:
            self._send_chat_fallback("five_attendance_failures", lesson_id)

    def _clear_pending_for_lesson(self, lesson_id: str | None):
        for pending_event_id, pending in tuple(self.pending_qr.items()):
            if pending.lesson_id == lesson_id:
                self.pending_qr.pop(pending_event_id, None)
                self.retry_attempts.pop(pending_event_id, None)
        self.latest_qr_event_by_lesson.pop(lesson_id, None)
        self.attendance_failures_by_lesson.pop(lesson_id, None)

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
        if not self.browser.is_running:
            log.warning("chat_fallback_skipped reason=%s cause=browser_unavailable", reason)
            return
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
        self.tray.showMessage(
            "Чат MTS Link недоступен",
            message,
            QSystemTrayIcon.MessageIcon.Warning,
            8000,
        )

    def closeEvent(self, event):
        if self.force_exit or not QSystemTrayIcon.isSystemTrayAvailable():
            event.accept()
        else:
            event.ignore()
            self.hide()
            self.tray.showMessage(
                "MIREA Lecture Assistant",
                "Приложение продолжает работать в фоне.",
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )

    def _restore(self):
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def _quit(self):
        log.info("quit_requested")
        self.force_exit = True
        self.tray.hide()
        try:
            self.browser.close()
        except Exception:
            log.warning("browser_close_on_quit_failed", exc_info=True)
        QApplication.quit()
