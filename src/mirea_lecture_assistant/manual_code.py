"""Codes for the student's own sign-ins: from the mailbox to the clipboard and the page.

The app reads MIREA codes from the mailbox for its own sign-ins already. When the
student signs in by hand in a browser, the same letter used to mean a trip to the
mailbox. A watcher now notices a new MIREA code letter, and the window copies
the code and, when the browser in front shows a MIREA page, types it in.

Codes the app requested itself are not touched: the window ignores letters that
arrive while one of its own sign-ins runs and shortly after.
"""

from __future__ import annotations

import imaplib
import logging
import select
import sys
import threading
import time
from datetime import UTC, datetime

from .email_otp import (
    EmailAccount,
    ImapOtpReader,
    _authenticated_mailbox,
    _is_authentication_error,
    _junk_folders,
)

log = logging.getLogger(__name__)

# The server tells about a new letter itself (IMAP IDLE): no polling, no traffic
# while nothing arrives. IDLE is renewed this often, as servers drop a silent one
# after 10-30 minutes; spam folders, which IDLE does not watch, are read then too.
IDLE_REFRESH_SECONDS = 5 * 60
# A server without IDLE is read this often instead.
FALLBACK_POLL_SECONDS = 60
# After a failure the connection is opened again later, and later still if it keeps failing.
RECONNECT_SECONDS = (30, 120, 600)
# A wrong mailbox password is not retried every few seconds.
AUTH_FAILURE_PAUSE_SECONDS = 30 * 60
# Windows of desktop browsers: Chrome, Edge, Яндекс, Opera, Brave, Vivaldi; Firefox.
BROWSER_WINDOW_CLASSES = {"Chrome_WidgetWin_1", "MozillaWindowClass"}
# Discord, VS Code, Slack and other Electron apps share Chrome's window class, so the
# program itself must be a browser too: a code never lands in a chat named «МИРЭА».
BROWSER_EXECUTABLES = {
    "chrome.exe",
    "chromium.exe",
    "msedge.exe",
    "browser.exe",  # Яндекс Браузер
    "opera.exe",
    "brave.exe",
    "vivaldi.exe",
    "arc.exe",
    "thorium.exe",
    "firefox.exe",
    "librewolf.exe",
    "waterfox.exe",
    "floorp.exe",
    "zen.exe",
}
MIREA_TITLE_MARKERS = ("mirea", "мирэа")


class CodeWatcher:
    """Reports each new MIREA code letter once, from a thread of its own."""

    def __init__(self, account: EmailAccount, on_code, reader: ImapOtpReader | None = None):
        self.account = account.normalized()
        self.on_code = on_code
        self.reader = reader or ImapOtpReader()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="mirea-code-watcher", daemon=True)
        self._thread.start()
        log.info("manual_code_watcher_started provider=%s", self.account.provider)

    def stop(self) -> None:
        self._stop.set()
        log.info("manual_code_watcher_stopped")

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def _run(self) -> None:
        # Only letters that arrive from now on: a code in the mailbox already is spent.
        started = datetime.now(UTC)
        after_uid: int | None = None
        checked: set[tuple[str, bytes]] = set()
        failures = 0
        while not self._stop.is_set():
            try:
                if after_uid is None:
                    after_uid = self.reader.latest_uid(self.account)
                with _authenticated_mailbox(self.account) as mailbox:
                    folders = ["INBOX", *_junk_folders(mailbox)]
                    idle = "IDLE" in getattr(mailbox, "capabilities", ())
                    log.info("manual_code_watcher_connected idle=%s", idle)
                    failures = 0
                    while not self._stop.is_set():
                        code = self.reader._poll(
                            mailbox,
                            folders,
                            started,
                            after_uid,
                            checked,
                            {},
                            False,
                            sign_in_only=True,
                        )
                        # Letters below the newest one seen need no second look.
                        inbox = [int(uid) for folder, uid in checked if folder == "INBOX"]
                        if inbox:
                            after_uid = max(after_uid, *inbox)
                            checked = {key for key in checked if key[0] != "INBOX"}
                        if code and not self._stop.is_set():
                            log.info("manual_code_letter_found")
                            self.on_code(code)
                            continue  # another new letter may be waiting
                        if idle:
                            mailbox.select("INBOX", readonly=True)
                            idle_wait(mailbox, IDLE_REFRESH_SECONDS, self._stop)
                        else:
                            self._stop.wait(FALLBACK_POLL_SECONDS)
            except Exception as exc:  # noqa: BLE001 - the watcher must outlive any mailbox error
                if self._stop.is_set():
                    return
                # latest_uid turns a rejected password into advice on app passwords.
                rejected = (
                    isinstance(exc, imaplib.IMAP4.error) and _is_authentication_error(exc)
                ) or (isinstance(exc, RuntimeError) and "парол" in str(exc).casefold())
                if rejected:
                    pause = AUTH_FAILURE_PAUSE_SECONDS
                else:
                    pause = RECONNECT_SECONDS[min(failures, len(RECONNECT_SECONDS) - 1)]
                failures += 1
                log.warning(
                    "manual_code_watcher_failed error=%s retry_seconds=%s",
                    type(exc).__name__,
                    pause,
                )
                self._stop.wait(pause)


def _socket_ready(sock, timeout: float) -> bool:
    # Bytes TLS has decrypted already are not visible to select().
    if getattr(sock, "pending", lambda: 0)():
        return True
    readable, _, _ = select.select([sock], [], [], timeout)
    return bool(readable)


def idle_wait(mailbox, seconds: float, stop: threading.Event, ready=_socket_ready) -> bool:
    """IMAP IDLE (RFC 2177): sleep until the server reports a change, or ``seconds`` pass.

    Python 3.12's imaplib has no IDLE, so the few lines are spoken here. The thread
    sleeps in select(); it wakes once a second only to notice ``stop``. Returns
    True when the server reported something (a new letter, usually).
    """
    tag = mailbox._new_tag()
    mailbox.send(tag + b" IDLE\r\n")
    reply = mailbox.readline()
    if not reply.startswith(b"+"):
        raise imaplib.IMAP4.error(f"IDLE refused: {reply[:60]!r}")
    deadline = time.monotonic() + seconds
    changed = False
    try:
        while not stop.is_set() and time.monotonic() < deadline:
            if not ready(mailbox.sock, 1.0):
                continue
            line = mailbox.readline()
            if not line:
                raise imaplib.IMAP4.abort("the mail server closed the connection")
            if line.startswith(b"*") and (b"EXISTS" in line or b"RECENT" in line):
                changed = True
                break
    finally:
        mailbox.send(b"DONE\r\n")
        while True:
            line = mailbox.readline()
            if not line:
                raise imaplib.IMAP4.abort("the mail server closed the connection")
            if line.startswith(tag):
                break
    return changed


def looks_like_mirea_page(title: str, window_class: str, executable: str) -> bool:
    """A desktop browser whose page title names MIREA (its sign-in page does)."""
    folded = title.casefold()
    return (
        window_class in BROWSER_WINDOW_CLASSES
        and executable.casefold() in BROWSER_EXECUTABLES
        and any(marker in folded for marker in MIREA_TITLE_MARKERS)
    )


def foreground_window() -> tuple[str, str, int, str] | None:
    """(title, window class, process id, program file name) of the window in front."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    window = user32.GetForegroundWindow()
    if not window:
        return None
    title = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(window, title, len(title))
    window_class = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(window, window_class, len(window_class))
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
    return title.value, window_class.value, int(pid.value), _program_name(int(pid.value))


def _program_name(pid: int) -> str:
    """The file name of a process's program, or "" when Windows does not tell."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    process = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        return ""
    try:
        path = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(path))
        if not kernel32.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)):
            return ""
        return path.value.replace("/", "\\").rsplit("\\", 1)[-1]
    finally:
        kernel32.CloseHandle(process)


def type_text(text: str) -> bool:
    """Type ``text`` into the focused field as key presses (Windows only)."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    keyboard_input, keyevent_unicode, keyevent_keyup = 1, 0x0004, 0x0002

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class INPUTUNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("union", INPUTUNION)]

    events = []
    for character in text:
        for flags in (keyevent_unicode, keyevent_unicode | keyevent_keyup):
            event = INPUT(type=keyboard_input)
            event.union.ki = KEYBDINPUT(0, ord(character), flags, 0, 0)
            events.append(event)
    array = (INPUT * len(events))(*events)
    sent = ctypes.windll.user32.SendInput(len(events), array, ctypes.sizeof(INPUT))
    return sent == len(events)


def copy_secret(text: str) -> bool:
    """Put ``text`` on the Windows clipboard, kept out of its history and cloud sync.

    Straight through the Win32 API: Qt's QMimeData given to the clipboard crashed
    the process when Qt shut down with it still there, and a crash on «Выход»
    would have the watchdog start the app again.
    """
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    # 64-bit handles and pointers: without these ctypes cuts them to 32 bits.
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterClipboardFormatW.restype = wintypes.UINT

    def global_copy(data: bytes):
        handle = kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            kernel32.GlobalFree(handle)
            return None
        ctypes.memmove(pointer, data, len(data))
        kernel32.GlobalUnlock(handle)
        return handle

    for _attempt in range(10):  # another program may hold the clipboard a moment
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return False
    try:
        user32.EmptyClipboard()
        never = (0).to_bytes(4, "little")
        items = [(13, text.encode("utf-16-le") + b"\0\0")]  # CF_UNICODETEXT
        for name, value in (
            ("ExcludeClipboardContentFromMonitorProcessing", b"\x01"),
            ("CanIncludeInClipboardHistory", never),
            ("CanUploadToCloudClipboard", never),
        ):
            items.append((user32.RegisterClipboardFormatW(name), value))
        for clipboard_format, data in items:
            handle = global_copy(data)
            # The clipboard owns the memory once it takes it; free it otherwise.
            if handle and not user32.SetClipboardData(clipboard_format, handle):
                kernel32.GlobalFree(handle)
        return True
    finally:
        user32.CloseClipboard()


def own_code_window_is_open(own_until: float, login_running: bool) -> bool:
    return login_running or time.monotonic() < own_until
