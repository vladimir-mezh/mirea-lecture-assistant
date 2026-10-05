from __future__ import annotations

import contextlib
import imaplib
import threading

import pytest

from mirea_lecture_assistant import manual_code
from mirea_lecture_assistant.email_otp import EmailAccount


class IdleServer:
    """A mailbox that answers IDLE the way RFC 2177 servers do."""

    def __init__(self, pushes, *, accept=True):
        self.pushes = list(pushes)  # untagged lines the server sends while idling
        self.accept = accept
        self.sent: list[bytes] = []
        self.sock = object()
        self.replies: list[bytes] = []

    def _new_tag(self):
        return b"A001"

    def send(self, data):
        self.sent.append(data)
        if data.endswith(b"IDLE\r\n"):
            self.replies.append(b"+ idling\r\n" if self.accept else b"A001 BAD no idle\r\n")
        elif data == b"DONE\r\n":
            self.replies.append(b"A001 OK IDLE terminated\r\n")

    def readline(self):
        if self.replies:
            return self.replies.pop(0)
        return self.pushes.pop(0)


def test_a_new_letter_wakes_the_wait_at_once():
    server = IdleServer([b"* 12 EXISTS\r\n"])
    ready = iter([False, False, True])

    changed = manual_code.idle_wait(
        server, 300, threading.Event(), ready=lambda _sock, _timeout: next(ready)
    )

    assert changed
    assert server.sent == [b"A001 IDLE\r\n", b"DONE\r\n"]  # IDLE is always closed
    assert server.replies == []  # the tagged end of IDLE was read, nothing left over


def test_a_quiet_mailbox_costs_nothing_until_the_refresh(monkeypatch):
    server = IdleServer([])
    clock = iter([0, 0, 100, 200, 301])
    monkeypatch.setattr(manual_code.time, "monotonic", lambda: next(clock))

    changed = manual_code.idle_wait(
        server, 300, threading.Event(), ready=lambda _sock, _timeout: False
    )

    assert not changed
    assert server.sent[-1] == b"DONE\r\n"


def test_stopping_ends_the_wait():
    server = IdleServer([])
    stop = threading.Event()

    def ready(_sock, _timeout):
        stop.set()
        return False

    assert not manual_code.idle_wait(server, 300, stop, ready=ready)
    assert server.sent[-1] == b"DONE\r\n"


def test_a_server_without_idle_is_reported():
    with pytest.raises(imaplib.IMAP4.error):
        manual_code.idle_wait(IdleServer([], accept=False), 300, threading.Event())


@pytest.mark.parametrize(
    ("title", "window_class", "program", "expected"),
    [
        ("Вход в МИРЭА - Google Chrome", "Chrome_WidgetWin_1", "chrome.exe", True),
        ("Вход в МИРЭА — Яндекс Браузер", "Chrome_WidgetWin_1", "browser.exe", True),
        ("Вход в МИРЭА - Microsoft Edge", "Chrome_WidgetWin_1", "msedge.exe", True),
        ("Sign in to mirea — Mozilla Firefox", "MozillaWindowClass", "firefox.exe", True),
        ("Входящие — Почта Mail.ru - Google Chrome", "Chrome_WidgetWin_1", "chrome.exe", False),
        # Electron apps share Chrome's window class: a chat named МИРЭА is no sign-in page.
        ("#мирэа - Discord", "Chrome_WidgetWin_1", "Discord.exe", False),
        ("МИРЭА — Visual Studio Code", "Chrome_WidgetWin_1", "Code.exe", False),
        ("МИРЭА — Блокнот", "Notepad", "notepad.exe", False),
        ("Вход в МИРЭА - Google Chrome", "Chrome_WidgetWin_1", "", False),  # unknown program
    ],
)
def test_codes_are_typed_only_into_a_browser_showing_mirea(title, window_class, program, expected):
    assert manual_code.looks_like_mirea_page(title, window_class, program) is expected


def test_the_watcher_reports_each_new_code_once_and_sleeps_in_idle(monkeypatch):
    found = []

    class Reader:
        polls = iter([None, "111111", None, "222222", None])

        def latest_uid(self, _account):
            return 40

        def _poll(self, _mailbox, folders, _since, after_uid, checked, _foreign, foreign_ok):
            assert folders == ["INBOX"] and after_uid >= 40 and foreign_ok is False
            return next(self.polls, None)

    class Mailbox:
        capabilities = ("IMAP4REV1", "IDLE")

        def select(self, *_args, **_kwargs):
            return "OK", [b"1"]

    @contextlib.contextmanager
    def connect(_account):
        yield Mailbox()

    idles = []

    def idle(_mailbox, seconds, stop):
        idles.append(seconds)
        if len(idles) == 3:
            watcher.stop()
        return True

    monkeypatch.setattr(manual_code, "_authenticated_mailbox", connect)
    monkeypatch.setattr(manual_code, "_junk_folders", lambda _mailbox: [])
    monkeypatch.setattr(manual_code, "idle_wait", idle)
    account = EmailAccount("me@yandex.ru", "app-password")
    watcher = manual_code.CodeWatcher(account, found.append, reader=Reader())

    watcher._run()

    assert found == ["111111", "222222"]
    assert idles == [manual_code.IDLE_REFRESH_SECONDS] * 3  # no polling loop in between
