from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from .async_runtime import run_async
from .cdp import (
    DEEP_ALL_JS,
    Browser,
    CdpError,
    CdpTimeout,
    browser_id,
    debugger_url,
    endpoint_alive,
)
from .moodle import looks_like_login_page

log = logging.getLogger(__name__)

# Capturing a full-resolution frame of a live stream is slower than a static page.
CAPTURE_TIMEOUT_MS = 10_000
# Room-state phrases are read from the page; a status banner is a short line.
# Anything longer is a sentence someone typed.
STATUS_LINE_MAX = 80
# Chat and comment panes: whatever students type there is not the room's state.
# Status banners are often aria-live regions themselves, so those are kept.
CHAT_SELECTOR = "[class*='chat' i], [class*='comment' i], [role='log']"
VISIBLE_TEXT_WITHOUT_CHAT = (
    """
selector => {"""
    + DEEP_ALL_JS
    + """
  const chat = new Set();
  for (const element of deepAll(selector)) {
    for (const line of (element.innerText || '').split('\\n')) {
      const text = line.trim();
      if (text) chat.add(text);
    }
  }
  // The page and the frames of the same site, where a room may be rendered.
  const bodies = deepAll('body').map(body => body.innerText || '');
  return bodies.join('\\n')
    .split('\\n')
    .filter(line => !chat.has(line.trim()))
    .join('\\n');
}
"""
)
CHAT_TEXT = "s => {" + DEEP_ALL_JS + " return deepAll(s).map(e => e.innerText || '').join('\\n'); }"


class NotSignedInError(RuntimeError):
    """The page came back as a sign-in form instead of content."""


# Controls that can take a visitor from a lobby into the room.
ENTRY_CONTROLS = "button, a, [role='button'], input[type='submit'], input[type='button']"
NAME_FIELDS = "input[type='text'], input:not([type])"
CHAT_EDITORS = "textarea, input, [contenteditable='true']"
CHAT_BUTTONS = "button, [role='button']"
CHAT_PLACEHOLDER_RE = re.compile(r"введите сообщение", re.IGNORECASE)
# Chrome throttles and stops painting background, minimised or covered windows;
# the lecture is captured exactly while it is in the background.
BACKGROUND_FLAGS = (
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
)


LIVENESS_TIMEOUT_SECONDS = 2.0


class BrowserService:
    """Launch a dedicated Chrome/Edge profile and access the lecture through CDP."""

    WINDOW_MARKER = "[MIREA Lecture]"
    PORT_FILE = ".mirea-cdp-port"
    LAUNCH_TIMEOUT_SECONDS = 12
    BLANK_PAGE = "about:blank"
    DISCONNECTED_RE = re.compile(
        r"соединение\s+(?:потеряно|прервано)|переподключ|connection\s+lost|disconnected|"
        r"страница\s+недоступна|не\s+уда[её]тся\s+получить\s+доступ|err_(?:connection|network)",
        re.IGNORECASE,
    )
    # The /j/ invitation lands on an entry page, not inside the room: until its
    # control is pressed the scanner would be watching a waiting screen. Only a
    # whole short label counts: "Подключиться по телефону", "Вступить в группу"
    # or a chat link ending in /join are not the way into the room.
    JOIN_RE = re.compile(
        r"(?:подключиться|присоединиться|войти|вступить)"
        r"(?:\s+(?:в|к)\s+(?:комнату|мероприятию|мероприятие|вебинару|вебинар|трансляции))?|"
        r"начать\s+(?:просмотр|трансляцию)|join(?:\s+(?:now|event|webinar|meeting))?|enter",
        re.IGNORECASE,
    )
    # "Подключить микрофон" also says "подключ"; pressing it would switch on the mic.
    DEVICE_RE = re.compile(
        r"микрофон|камер|звук|динамик|гарнитур|наушник|экран|устройств|"
        r"microphone|camera|audio|speaker|screen|device",
        re.IGNORECASE,
    )
    JOIN_LABEL_MAX = 40
    ENDED_RE = re.compile(
        r"(?:встреча|мероприятие|трансляция|вебинар)\s+(?:заверш[её]н[ао]?|окончен[ао]?)|"
        r"спасибо\s+за\s+участие|(?:meeting|event|webinar)\s+(?:has\s+)?ended",
        re.IGNORECASE,
    )

    def __init__(self, profile_dir: Path):
        self.profile_dir = profile_dir
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.port: int | None = None
        self.process: subprocess.Popen | None = None
        self.muted: bool | None = None
        self.lecture_url: str | None = None
        # Capture resolution is deliberately independent of the window size: a
        # 520x360 window screenshots at ~504x265, where a QR in the stream is
        # far too small to decode.
        self.capture_size: tuple[int, int] | None = (1920, 1080)
        self._browser: Browser | None = None
        # The lecture tab by its DevTools id: never "whatever tab is newest".
        self._lecture_target: str | None = None
        # Tabs the service opened to read the СДО; never taken for the lecture.
        self._helper_targets: set[str] = set()
        # Chrome's id for the browser run this service launched or adopted.
        self._browser_id: str | None = None
        self._connect_lock: asyncio.Lock | None = None
        self._connect_lock_loop = None
        # (page, size, CDP session) of the viewport override currently in force.
        self._capture_override = None

    @property
    def is_running(self) -> bool:
        if self.port is not None and self._cdp_available(self.port):
            return True
        discovered = self._discover_port()
        if discovered is not None:
            log.info("browser_attached port_discovered=%s", discovered)
            self.port = discovered
            return True
        return False

    @property
    def probably_running(self) -> bool:
        """A cheap guess for the GUI thread; ``is_running`` probes over HTTP."""
        return self.port is not None or self._browser is not None

    def _discover_port(self) -> int | None:
        """Adopt a browser this profile already runs, started by an earlier session.

        Without this, a second BrowserService cannot see the first one's browser,
        tries to launch its own on the same profile, and Chrome refuses. Chrome
        only writes DevToolsActivePort when the port is left to it, so the port
        this service picked is recorded next to it.
        """
        for name in (self.PORT_FILE, "DevToolsActivePort"):
            try:
                first_line = (self.profile_dir / name).read_text(encoding="utf-8").splitlines()[0]
            except (OSError, IndexError):
                continue
            port = first_line.strip()
            if not port.isdigit():
                continue
            # The browser run recorded with the port must be the one answering:
            # another Chrome or an Electron app reusing the port must never get
            # our clicks and typing.
            previous, self._browser_id = self._browser_id, self._recorded_browser_id(name)
            if self._cdp_available(int(port)):
                return int(port)
            self._browser_id = previous
        return None

    def _find_browser(self) -> tuple[str, str]:
        candidates: list[tuple[str, str]] = []
        if sys.platform == "win32":
            roots = [
                os.environ.get("PROGRAMFILES"),
                os.environ.get("PROGRAMFILES(X86)"),
                os.environ.get("LOCALAPPDATA"),
            ]
            for root in filter(None, roots):
                candidates.extend(
                    [
                        ("Google Chrome", str(Path(root) / "Google/Chrome/Application/chrome.exe")),
                        (
                            "Microsoft Edge",
                            str(Path(root) / "Microsoft/Edge/Application/msedge.exe"),
                        ),
                    ]
                )
        elif sys.platform == "darwin":
            candidates.extend(
                [
                    (
                        "Google Chrome",
                        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    ),
                    ("Chromium", "/Applications/Chromium.app/Contents/MacOS/Chromium"),
                ]
            )
        else:
            for command, name in (
                ("google-chrome", "Google Chrome"),
                ("chromium", "Chromium"),
                ("chromium-browser", "Chromium"),
                ("microsoft-edge", "Microsoft Edge"),
            ):
                if path := shutil.which(command):
                    candidates.append((name, path))
        for name, path in candidates:
            if Path(path).is_file():
                return name, path
        raise RuntimeError("Не найден Google Chrome, Microsoft Edge или Chromium")

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _cdp_available(self, port: int) -> bool:
        # A closed port is refused at once, so the wait only matters for a live
        # browser that is slow to answer (a heavy page, a busy machine): with 0.3 s
        # it passed for dead, and a second browser was started on its profile.
        return endpoint_alive(port, LIVENESS_TIMEOUT_SECONDS, expected_id=self._browser_id)

    def ensure_running(self) -> str | None:
        """Start the profile's browser on a blank tab, leaving any lecture alone."""
        if self.is_running:
            return None
        return self.open(self.BLANK_PAGE, muted=True, width=1100, height=760)

    def _recorded_browser_id(self, name: str) -> str | None:
        try:
            lines = (self.profile_dir / name).read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        if len(lines) < 2 or not lines[1].strip():
            return None
        return lines[1].strip().rsplit("/", 1)[-1]

    def _remember_port(self, port: int) -> None:
        try:
            self._browser_id = browser_id(debugger_url(port))
        except (OSError, CdpError, ValueError, KeyError):
            self._browser_id = None
        record = f"{port}\n{self._browser_id}" if self._browser_id else str(port)
        try:
            (self.profile_dir / self.PORT_FILE).write_text(record, encoding="utf-8")
        except OSError:
            log.debug("cdp_port_not_recorded", exc_info=True)

    def open(
        self,
        url: str,
        *,
        muted: bool = True,
        width: int = 520,
        height: int = 360,
        force_navigation: bool = False,
    ) -> str:
        blank = url == self.BLANK_PAGE
        if not blank and not url.lower().startswith(("https://", "http://")):
            raise ValueError("Ссылка на лекцию должна начинаться с https:// или http://")
        browser_name, executable = self._find_browser()
        log.info(
            "browser_open_requested browser=%s host=%s muted=%s size=%sx%s",
            browser_name,
            urlparse(url).hostname or "unknown",
            muted,
            width,
            height,
        )
        # A browser adopted from an earlier run has an unknown sound mode; killing
        # it on that guess left the profile locked and no tab controllable at all.
        if self.is_running and self.muted is not None and self.muted != muted:
            self.close()
        if not self.is_running:
            self.port = self._free_port()
            args = [
                executable,
                f"--remote-debugging-port={self.port}",
                f"--user-data-dir={self.profile_dir}",
                f"--window-size={width},{height}",
                "--window-position=20,20",
                "--no-first-run",
                "--no-default-browser-check",
                *BACKGROUND_FLAGS,
            ]
            if muted:
                args.append("--mute-audio")
            args.append(url)
            self.process = subprocess.Popen(args, close_fds=True)
            self.muted = muted
            deadline = time.monotonic() + self.LAUNCH_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                if self._cdp_available(self.port):
                    break
                time.sleep(0.15)
            else:
                # Recording the port before this point overwrote a working one with
                # a dead guess, and the next run could no longer find the browser.
                self.port = None
                raise RuntimeError(f"{browser_name} запущен, но управление вкладкой недоступно")
            self._remember_port(self.port)
        elif not blank:
            self._navigate(url, force_navigation=force_navigation)
        if not blank:
            self.lecture_url = url
        return browser_name

    def restart(self, url: str, *, muted: bool = True, width: int = 520, height: int = 360) -> str:
        """Escalate recovery by replacing the dedicated browser process."""
        log.warning("browser_restart_requested")
        process = self.process
        try:
            if self.is_running:
                run_async(self._close_async(), timeout=6)
        except Exception:
            log.warning("browser_graceful_restart_failed", exc_info=True)
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    log.warning("browser_process_kill_failed", exc_info=True)
        (self.profile_dir / self.PORT_FILE).unlink(missing_ok=True)
        self.port = None
        self.process = None
        self.muted = None
        return self.open(url, muted=muted, width=width, height=height)

    def _navigate(self, url: str, *, force_navigation: bool = False) -> None:
        run_async(self._navigate_async(url, force_navigation=force_navigation))

    @staticmethod
    def _room_key(url: str) -> tuple[str, str] | None:
        """Identify an MTS room across its invitation and in-meeting URLs."""
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host != "mts-link.ru" and not host.endswith(".mts-link.ru"):
            return None
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 3 and parts[0] == "j" and parts[2].isdigit():
            return "mts", parts[2]
        if len(parts) >= 2 and parts[0] == "event" and parts[1].isdigit():
            return "mts", parts[1]
        return None

    async def _navigate_async(self, url: str, *, force_navigation: bool = False) -> None:
        browser = await self._connected_browser()
        room_key = self._room_key(url)
        if room_key is not None:
            matching = [
                candidate
                for candidate in browser.pages
                if not candidate.is_closed() and self._room_key(candidate.url) == room_key
            ]
            if matching:
                existing = matching[-1]
                self._lecture_target = existing.target_id
                if force_navigation:
                    # Reload the actual webinar instead of reopening its /j/
                    # invitation, which makes MTS Link spawn another tab.
                    await existing.reload(timeout=15_000)
                    log.info("lecture_tab_reloaded room=%s", room_key[1])
                else:
                    log.info("lecture_tab_reused room=%s", room_key[1])
                return
        # The previous lecture's tab, a blank tab of ours, or a new one — never
        # a helper tab reading the СДО or a tab the student opened.
        page = self._pick_lecture_page(browser.pages) or next(
            (
                candidate
                for candidate in browser.pages
                if candidate.url in ("", self.BLANK_PAGE)
                and candidate.target_id not in self._helper_targets
            ),
            None,
        )
        if page is None:
            page = await browser.new_page()
        self._lecture_target = page.target_id
        await page.goto(url)

    def _pick_lecture_page(self, pages: list):
        """Choose the lecture tab by host instead of trusting tab order.

        Helper tabs (reading the СДО, a popup opened by the platform) would
        otherwise become the capture target and the QR scanner would silently
        watch the wrong page.
        """
        if not pages:
            return None
        if self._lecture_target:
            for page in pages:
                if getattr(page, "target_id", None) == self._lecture_target:
                    return page
        if self.lecture_url:
            matching = [page for page in pages if self._matches_lecture_url(page.url)]
            if matching:
                self._lecture_target = getattr(matching[-1], "target_id", None)
                return matching[-1]
        return None

    def _matches_lecture_url(self, url: str) -> bool:
        current = (urlparse(url).hostname or "").lower()
        wanted = (urlparse(self.lecture_url or "").hostname or "").lower()
        if not current or not wanted:
            return False
        current_room = self._room_key(url)
        wanted_room = self._room_key(self.lecture_url or "")
        if current_room is not None and wanted_room is not None:
            return current_room == wanted_room
        if current == wanted:
            return True
        return self.is_mts(current) and self.is_mts(wanted)

    @staticmethod
    def is_mts(host: str) -> bool:
        return host == "mts-link.ru" or host.endswith(".mts-link.ru")

    def lecture_is_open(self) -> bool:
        """Whether the current lecture still has a live, connected browser tab."""
        return self.lecture_state() == "live"

    def lecture_state(self) -> str:
        """Return ``live``, ``ended`` or ``lost`` for the current lecture tab."""
        if not self.is_running or not self.lecture_url:
            return "lost"
        try:
            return run_async(self._lecture_state_async(), timeout=6)
        except Exception:
            log.warning("lecture_health_check_failed", exc_info=True)
            return "lost"

    async def _lecture_state_async(self) -> str:
        browser = await self._connected_browser()
        page = self._pick_lecture_page(browser.pages)
        if page is None or page.is_closed() or not self._matches_lecture_url(page.url):
            return "lost"
        try:
            text = await page.evaluate(VISIBLE_TEXT_WITHOUT_CHAT, CHAT_SELECTOR, timeout=4)
        except CdpTimeout:
            return "lost"  # the page does not answer at all
        except CdpError:  # a transient DOM update is not proof that the tab died
            return "live"
        state = self.room_state_from_text(text)
        if state == "lost":
            # "Переподключение…" is often over in seconds; the caller acts on it
            # only when it is still there at the next check.
            return "unstable"
        if state is not None:
            return state
        index, label = await self._entry_control(page)
        if index is not None:
            log.info("lecture_entry_pending label=%s", label)
            return "waiting"
        return "live"

    @classmethod
    def room_state_from_text(cls, text: str) -> str | None:
        """``ended`` or ``lost`` when a status banner says so, otherwise None.

        Only short standalone lines count, and never questions: a student typing
        "вебинар завершён?" or "переподключитесь" in the chat must not close the
        lecture or restart the browser.
        """
        for raw_line in text.splitlines():
            line = " ".join(raw_line.split())
            if not line or len(line) > STATUS_LINE_MAX or "?" in line:
                continue
            if cls.ENDED_RE.search(line):
                return "ended"
            if cls.DISCONNECTED_RE.search(line):
                return "lost"
        return None

    @classmethod
    def is_entry_label(cls, label: str) -> bool:
        label = " ".join(label.split()).strip(" .!→>»")
        return (
            bool(label)
            and len(label) <= cls.JOIN_LABEL_MAX
            and bool(cls.JOIN_RE.fullmatch(label))
            and not cls.DEVICE_RE.search(label)
        )

    async def _entry_control(self, page) -> tuple[int | None, str]:
        """Find the visible control that takes this page from the lobby into the room."""
        for element in await page.elements(ENTRY_CONTROLS):
            # Controls in the chat, and links leaving the site, are what other
            # people wrote or linked to — not the platform's own way in.
            if not element["visible"] or element.get("chat") or element.get("external"):
                continue
            label = (element["text"] or element["value"]).strip()
            if self.is_entry_label(label):
                return element["index"], label[:60]
        return None, ""

    def join_lecture(self, display_name: str = "") -> str:
        """Press the platform's own entry control. Returns joined / already / refused."""
        return run_async(self._join_lecture_async(display_name))

    async def _join_lecture_async(self, display_name: str) -> str:
        page = await self._active_page()
        host = (urlparse(page.url).hostname or "").lower()
        if not self.is_mts(host):
            # Clicking unknown controls on an unknown page is never worth the risk.
            log.info("lecture_join_skipped host=%s", host or "unknown")
            return "refused"
        index, label = await self._entry_control(page)
        if index is None:
            return "already"
        if display_name:
            await self._fill_display_name(page, display_name)
        await page.click(ENTRY_CONTROLS, index=index, expect_label=label)
        log.info("lecture_join_clicked label=%s", label)
        await page.wait_for_timeout(2_000)
        return "joined"

    async def _fill_display_name(self, page, display_name: str) -> None:
        """Lobbies ask who is entering; an empty field keeps the button disabled."""
        for element in await page.elements(NAME_FIELDS):
            if not element["visible"] or element["value"]:
                continue
            try:
                await page.fill(NAME_FIELDS, display_name, index=element["index"])
            except CdpError:  # a read-only or decorative field is fine to skip
                log.debug("entry_name_field_skipped", exc_info=True)
                continue
            log.info("lecture_join_name_filled")
            return

    async def _connected_browser(self) -> Browser:
        """One CDP connection for the whole session, reconnected only when it breaks.

        The scanner asks for a frame every couple of seconds; connecting anew for
        each one cost more than the capture itself and made frames time out.
        """
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        # Scan, health check and join may all find the connection broken at once;
        # without the lock each opened its own connection and all but one leaked.
        async with self._lock():
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            await self._release_connection()
            # The liveness probe is blocking HTTP; keep it off the shared loop.
            if not await asyncio.to_thread(lambda: self.is_running):
                raise RuntimeError("Браузер приложения не запущен")
            self._browser = await Browser.connect(self.port)
            log.info("cdp_connected port=%s", self.port)
            return self._browser

    def _lock(self) -> asyncio.Lock:
        """A lock bound to the running loop (tests run each call on a new loop)."""
        loop = asyncio.get_running_loop()
        if self._connect_lock is None or self._connect_lock_loop is not loop:
            self._connect_lock = asyncio.Lock()
            self._connect_lock_loop = loop
        return self._connect_lock

    async def _release_connection(self) -> None:
        """Drop the cached connection without closing the browser it controls."""
        browser, self._browser = self._browser, None
        self._capture_override = None
        if browser is None:
            return
        try:
            await browser.disconnect()
        except Exception:  # a connection that already died needs no goodbye
            log.debug("cdp_release_failed", exc_info=True)

    def disconnect(self) -> None:
        """Release the connection, leaving the browser and its session running."""
        run_async(self._release_connection())

    async def _active_page(self):
        browser = await self._connected_browser()
        page = self._pick_lecture_page(browser.pages)
        if page is None:
            raise RuntimeError("Вкладка лекции не открыта")
        return page

    async def _helper_page(self, browser):
        page = await browser.new_page(background=True)
        self._helper_targets.add(page.target_id)
        return page

    def read_html(self, url: str, *, timeout_ms: int = 20_000) -> str:
        """Read a page in a background tab of the same profile, then close it.

        The СДО refuses the MIREA SSO cookies the app stores, so its pages are
        read through the browser the user has signed in to once.
        """
        if not url.lower().startswith(("https://", "http://")):
            raise ValueError("Адрес источника должен начинаться с https:// или http://")
        html = run_async(self._read_html_async(url, timeout_ms))
        if looks_like_login_page(html):
            # Returning the sign-in page would read as "no webinars yet" further up.
            raise NotSignedInError(
                "Профиль браузера не вошёл в СДО: откройте online-edu.mirea.ru "
                "в окне приложения и войдите один раз"
            )
        return html

    def run_on_new_page(self, action, timeout: float | None = None):
        """Run one coroutine against a fresh background tab, then close it."""
        return run_async(self._run_on_new_page(action), timeout)

    async def _run_on_new_page(self, action):
        browser = await self._connected_browser()
        page = await self._helper_page(browser)
        try:
            return await action(page)
        finally:
            self._helper_targets.discard(page.target_id)
            await page.close()

    async def _read_html_async(self, url: str, timeout_ms: int) -> str:
        log.info("page_read_requested host=%s", urlparse(url).hostname or "unknown")
        browser = await self._connected_browser()
        page = await self._helper_page(browser)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                # Moodle renders course lists and webinar tables after load,
                # so the document alone is not yet the content we came for.
                await page.wait_for_network_idle(timeout=8_000)
            except CdpTimeout:
                log.debug("page_read_busy host=%s", urlparse(url).hostname)
            if "/mod/webinars/" in url:
                try:
                    # The table is filled in by a script after the document loads.
                    await page.wait_for_selector("#wb2-table table.data tbody tr", timeout=5_000)
                except CdpTimeout:  # a page with no webinars grows no rows
                    log.debug("page_read_no_table host=%s", urlparse(url).hostname)
            return await self._settled_content(page)
        finally:
            self._helper_targets.discard(page.target_id)
            await page.close()

    async def capture_png(self) -> bytes:
        """Capture page pixels directly, even when the window is overlapped."""
        png, _ = await self.capture_page_state()
        return png

    async def _apply_capture_size(self, page) -> None:
        """Render the page at the capture resolution, whatever the window size is.

        The emulated viewport also makes the platform request a higher quality
        stream, so the picture gains real detail instead of being upscaled.
        The override is set once per tab, on the tab's own DevTools session, which
        stays attached: sending it with every frame re-laid out the page each time.
        """
        current = self._capture_override
        if current is not None:
            applied_page, applied_size = current
            if applied_page is page and applied_size == self.capture_size and not page.is_closed():
                return
            self._capture_override = None
            if not applied_page.is_closed():
                try:
                    await applied_page.send("Emulation.clearDeviceMetricsOverride", {})
                except Exception:  # the old tab may be going away right now
                    log.debug("capture_override_release_failed", exc_info=True)
        if not self.capture_size:
            return
        width, height = self.capture_size
        await page.send(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
        )
        self._capture_override = (page, self.capture_size)

    async def capture_page_state(self) -> tuple[bytes, str]:
        """Capture pixels plus the chat's visible text for roll-call detection.

        Only the chat panes are read: the participant list shows classmates'
        display names ("Иванов Иван ИКБО-01-24"), which looked like a roll call.
        """
        page = await self._active_page()
        await self._apply_capture_size(page)
        # A 1920x1080 frame of a live lecture needs more than the 3.5s that
        # used to be allowed here: on 24.09 that budget lost 370 frames of 526.
        png = await page.screenshot(timeout=CAPTURE_TIMEOUT_MS)
        try:
            visible_text = await page.evaluate(CHAT_TEXT, CHAT_SELECTOR, timeout=1)
        except Exception:  # noqa: BLE001 - text observation must not break QR capture
            visible_text = ""
        return png, visible_text

    def close_lecture_tab(self) -> bool:
        """Close a finished lecture without killing the browser.

        The profile stays alive on a blank tab, so the СДО session it holds is
        still there for the next lookup and no video keeps playing in the
        background once the pair is over.
        """
        return run_async(self._close_lecture_tab_async())

    async def _close_lecture_tab_async(self) -> bool:
        if not await asyncio.to_thread(lambda: self.is_running):
            return False
        browser = await self._connected_browser()
        return await self._close_lecture_page(browser)

    async def _close_lecture_page(self, browser) -> bool:
        page = self._pick_lecture_page(browser.pages)
        if page is None:
            return False
        if len(browser.pages) == 1:
            # Closing the last tab would close the browser, and the СДО session
            # this profile holds would have to be established again.
            await browser.new_page()
        await page.close()
        self.lecture_url = None
        self._lecture_target = None
        log.info("lecture_tab_closed")
        return True

    def send_chat_message(self, message: str) -> None:
        """Send a message through the visible MTS Link chat using accessible labels."""
        if not message.strip():
            raise ValueError("Сообщение в чат пустое")
        log.info("chat_send_requested message_length=%s", len(message.strip()))
        run_async(self._send_chat_message_async(message.strip()))

    async def _send_chat_message_async(self, message: str) -> None:
        page = await self._active_page()
        host = (urlparse(page.url).hostname or "").lower()
        if host != "mts-link.ru" and not host.endswith(".mts-link.ru"):
            raise RuntimeError("Автоматическая отправка чата поддерживается только для MTS Link")

        editor = await self._visible_chat_editor(page)
        if editor is None:
            # The «Чат» button toggles the pane: pressing it while the chat is
            # already open used to close it and the message could not be typed.
            button = await self._chat_button(page)
            if button is not None:
                await page.click(CHAT_BUTTONS, index=button)
            for _ in range(6):
                editor = await self._visible_chat_editor(page)
                if editor is not None:
                    break
                await page.wait_for_timeout(250)
        if editor is None:
            raise RuntimeError("MTS Link не показал поле «Введите сообщение»")
        before = await page.count_text(message)
        await page.fill(CHAT_EDITORS, message, index=editor)
        uncertain = "Не удалось подтвердить доставку сообщения; повтор отключён во избежание дубля"
        try:
            await page.press_enter()
            for _ in range(10):
                await page.wait_for_timeout(500)
                if await page.count_text(message) > before:
                    log.info("chat_delivery_confirmed")
                    return
        except Exception as exc:
            # Enter may already have posted it; a retry would post it twice in public.
            raise RuntimeError(uncertain) from exc
        raise RuntimeError(uncertain)

    @staticmethod
    async def _visible_chat_editor(page) -> int | None:
        for element in await page.elements(CHAT_EDITORS):
            if element["visible"] and CHAT_PLACEHOLDER_RE.search(element["placeholder"]):
                return element["index"]
        return None

    @staticmethod
    async def _chat_button(page) -> int | None:
        """The button that opens the chat, preferring one named exactly «Чат»."""
        exact = re.compile(r"^\s*чат\s*$|открыть\s+чат", re.IGNORECASE)
        loose = re.compile(r"чат", re.IGNORECASE)
        buttons = [
            (element["index"], element["label"] or element["text"])
            for element in await page.elements(CHAT_BUTTONS)
            if element["visible"]
        ]
        for pattern in (exact, loose):
            for index, name in buttons:
                if pattern.search(name):
                    return index
        return None

    def close(self) -> None:
        """Close only the dedicated browser instance started by this service."""
        if not self.is_running:
            return
        run_async(self._close_async(), timeout=8)
        log.info("browser_closed")
        (self.profile_dir / self.PORT_FILE).unlink(missing_ok=True)
        self.port = None
        self.process = None
        self.muted = None

    async def _close_async(self) -> None:

        browser = await self._connected_browser()
        self._browser = None
        self._capture_override = None
        await browser.close_browser()

    def minimize(self) -> bool:
        """Minimize the controlled lecture window. Native implementation is Windows-first."""
        if not self.is_running or sys.platform != "win32":
            return False
        run_async(self._mark_title())
        for _ in range(5):
            if self._minimize_marked_windows():
                log.info("lecture_window_minimized")
                return True
            time.sleep(0.1)
        return False

    async def _mark_title(self) -> None:
        page = await self._active_page()
        await page.evaluate(
            "marker => { if (!document.title.startsWith(marker)) document.title = marker + ' ' + document.title; }",
            self.WINDOW_MARKER,
        )

    @staticmethod
    async def _settled_content(page, attempts: int = 3) -> str:
        """Read the DOM, retrying while the page is still redirecting.

        An SSO bounce makes `page.content()` fail outright rather than return the
        old document, so the read is repeated until the navigation settles.
        """
        for attempt in range(attempts):
            try:
                return await page.content()
            except CdpError:
                if attempt == attempts - 1:
                    raise
                log.debug("page_content_retry attempt=%s", attempt + 1)
                await page.wait_for_timeout(900)
        raise RuntimeError("Страница не перестала перезагружаться")

    def _minimize_marked_windows(self) -> bool:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found = False
        enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.EnumWindows.argtypes = [enum_proc_type, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL

        def callback(hwnd, _):
            nonlocal found
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                title = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, title, length + 1)
                if self.WINDOW_MARKER in title.value:
                    user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
                    found = True
            return True

        user32.EnumWindows(enum_proc_type(callback), 0)
        return found
