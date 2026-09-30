from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .async_runtime import run_async
from .moodle import looks_like_login_page

log = logging.getLogger(__name__)

# Capturing a full-resolution frame of a live stream is slower than a static page.
CAPTURE_TIMEOUT_MS = 10_000


class NotSignedInError(RuntimeError):
    """The page came back as a sign-in form instead of content."""


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
    # control is pressed the scanner would be watching a waiting screen.
    JOIN_RE = re.compile(
        r"подключ|присоедин|вступ|войти\s+в\s+(?:комнат|меропр|вебинар)|"
        r"начать\s+(?:просмотр|трансл)|\bjoin\b|\benter\b",
        re.IGNORECASE,
    )
    ENDED_RE = re.compile(
        r"(?:встреча|мероприятие|трансляция|вебинар)\s+(?:завершен[ао]?|окончено)|"
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
        self._playwright = None
        self._browser = None

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
            if port.isdigit() and self._cdp_available(int(port)):
                return int(port)
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

    @staticmethod
    def _cdp_available(port: int) -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=0.3):
                return True
        except (OSError, urllib.error.URLError):
            return False

    def ensure_running(self) -> str | None:
        """Start the profile's browser on a blank tab, leaving any lecture alone."""
        if self.is_running:
            return None
        return self.open(self.BLANK_PAGE, muted=True, width=1100, height=760)

    def _remember_port(self, port: int) -> None:
        try:
            (self.profile_dir / self.PORT_FILE).write_text(str(port), encoding="utf-8")
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
        page = await self._active_page()
        room_key = self._room_key(url)
        if room_key is not None:
            matching = [
                candidate
                for candidate in page.context.pages
                if not candidate.is_closed() and self._room_key(candidate.url) == room_key
            ]
            if matching:
                existing = matching[-1]
                if force_navigation:
                    # Reload the actual webinar instead of reopening its /j/
                    # invitation, which makes MTS Link spawn another tab.
                    await existing.reload(wait_until="domcontentloaded", timeout=15_000)
                    log.info("lecture_tab_reloaded room=%s", room_key[1])
                else:
                    log.info("lecture_tab_reused room=%s", room_key[1])
                return
        await page.goto(url)

    def _pick_lecture_page(self, pages: list):
        """Choose the lecture tab by host instead of trusting tab order.

        Helper tabs (reading the СДО, a popup opened by the platform) would
        otherwise become the capture target and the QR scanner would silently
        watch the wrong page.
        """
        if not pages:
            return None
        if self.lecture_url:
            matching = [page for page in pages if self._matches_lecture_url(page.url)]
            if matching:
                return matching[-1]
        return pages[-1]

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
        from playwright.async_api import Error as PlaywrightError

        page = await self._active_page()
        if page.is_closed() or not self._matches_lecture_url(page.url):
            return "lost"
        try:
            text = await page.locator("body").inner_text(timeout=1_000)
        except PlaywrightError:  # a transient DOM update is not proof that the tab died
            return "live"
        if self.ENDED_RE.search(text):
            return "ended"
        if self.DISCONNECTED_RE.search(text):
            return "lost"
        control, label = await self._entry_control(page)
        if control is not None:
            log.info("lecture_entry_pending label=%s", label)
            return "waiting"
        return "live"

    async def _entry_control(self, page):
        """Find the visible control that takes this page from the lobby into the room."""
        for handle in await page.query_selector_all(
            "button, a, [role='button'], input[type='submit'], input[type='button']"
        ):
            try:
                if not await handle.is_visible():
                    continue
                label = (await handle.inner_text()) or (await handle.get_attribute("value")) or ""
            except Exception:  # the lobby re-renders while being read
                log.debug("entry_control_unreadable", exc_info=True)
                continue
            label = label.strip()
            if label and self.JOIN_RE.search(label):
                return handle, label[:60]
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
        control, label = await self._entry_control(page)
        if control is None:
            return "already"
        if display_name:
            await self._fill_display_name(page, display_name)
        await control.click()
        log.info("lecture_join_clicked label=%s", label)
        await page.wait_for_timeout(2_000)
        return "joined"

    async def _fill_display_name(self, page, display_name: str) -> None:
        """Lobbies ask who is entering; an empty field keeps the button disabled."""
        for handle in await page.query_selector_all("input[type='text'], input:not([type])"):
            try:
                if not await handle.is_visible() or await handle.input_value():
                    continue
                await handle.fill(display_name)
            except Exception:  # a read-only or decorative field is fine to skip
                log.debug("entry_name_field_skipped", exc_info=True)
                continue
            else:
                log.info("lecture_join_name_filled")
                return

    async def _connected_browser(self):
        """One CDP connection for the whole session, reconnected only when it breaks.

        Starting a Playwright driver costs about 270 ms, and the scanner asks for
        a frame every couple of seconds: reconnecting each time spawned and killed
        a driver process thousands of times per lecture, which is what made frame
        captures time out.
        """
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        await self._release_connection()
        if not self.is_running:
            raise RuntimeError("Браузер приложения не запущен")
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{self.port}", timeout=3_500
            )
        except Exception:
            await self._release_connection()
            raise
        log.info("cdp_connected port=%s", self.port)
        return self._browser

    async def _release_connection(self) -> None:
        """Drop the cached connection without closing the browser it controls."""
        playwright, self._playwright, self._browser = self._playwright, None, None
        if playwright is None:
            return
        try:
            await playwright.stop()
        except Exception:  # a driver that already died needs no goodbye
            log.debug("cdp_release_failed", exc_info=True)

    def disconnect(self) -> None:
        """Release the connection, leaving the browser and its session running."""
        run_async(self._release_connection())

    async def _active_page(self):
        if not self.is_running:
            raise RuntimeError("Окно лекции ещё не открыто")
        context = (await self._connected_browser()).contexts[0]
        return self._pick_lecture_page(context.pages) or await context.new_page()

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
        if not self.is_running:
            raise RuntimeError("Браузер приложения не запущен")
        browser = await self._connected_browser()
        page = await browser.contexts[0].new_page()
        try:
            return await action(page)
        finally:
            await page.close()

    async def _read_html_async(self, url: str, timeout_ms: int) -> str:
        if not self.is_running:
            raise RuntimeError("Браузер приложения не запущен")
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        log.info("page_read_requested host=%s", urlparse(url).hostname or "unknown")
        browser = await self._connected_browser()
        page = await browser.contexts[0].new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                # Moodle renders course lists and webinar tables after load,
                # so the document alone is not yet the content we came for.
                await page.wait_for_load_state("networkidle", timeout=8_000)
            except PlaywrightTimeoutError:
                log.debug("page_read_busy host=%s", urlparse(url).hostname)
            if "/mod/webinars/" in url:
                try:
                    # The table is filled in by a script after the document loads.
                    await page.wait_for_selector("#wb2-table table.data tbody tr", timeout=5_000)
                except PlaywrightTimeoutError:  # a page with no webinars grows no rows
                    log.debug("page_read_no_table host=%s", urlparse(url).hostname)
            return await self._settled_content(page)
        finally:
            await page.close()

    async def capture_png(self) -> bytes:
        """Capture page pixels directly, even when the window is overlapped."""
        png, _ = await self.capture_page_state()
        return png

    async def _apply_capture_size(self, page) -> None:
        """Render the page at the capture resolution, whatever the window size is.

        The emulated viewport also makes the platform request a higher quality
        stream, so the picture gains real detail instead of being upscaled.
        """
        if not self.capture_size:
            return
        width, height = self.capture_size
        session = await page.context.new_cdp_session(page)
        try:
            await session.send(
                "Emulation.setDeviceMetricsOverride",
                {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
            )
        finally:
            await session.detach()

    async def capture_page_state(self) -> tuple[bytes, str]:
        """Capture pixels plus currently visible page text for chat signal detection."""
        page = await self._active_page()
        await self._apply_capture_size(page)
        # A 1920x1080 frame of a live lecture needs more than the 3.5s that
        # used to be allowed here: on 24.09 that budget lost 370 frames of 526.
        png = await page.screenshot(type="png", animations="disabled", timeout=CAPTURE_TIMEOUT_MS)
        try:
            visible_text = await page.locator("body").inner_text(timeout=1_000)
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
        if not self.is_running:
            return False

        browser = await self._connected_browser()
        return await self._close_lecture_page(browser.contexts[0])

    async def _close_lecture_page(self, context) -> bool:
        page = self._pick_lecture_page(context.pages)
        if page is None:
            return False
        if len(context.pages) == 1:
            # Closing the last tab would close the browser, and the СДО session
            # this profile holds would have to be established again.
            await context.new_page()
        await page.close()
        self.lecture_url = None
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

        chat_buttons = page.get_by_role("button", name=re.compile(r"чат", re.IGNORECASE))
        for index in range(await chat_buttons.count()):
            button = chat_buttons.nth(index)
            if await button.is_visible():
                await button.click()
                break

        editor = page.get_by_placeholder(re.compile(r"введите сообщение", re.IGNORECASE))
        visible_editor = None
        for index in range(await editor.count()):
            candidate = editor.nth(index)
            if await candidate.is_visible():
                visible_editor = candidate
                break
        if visible_editor is None:
            fallback = page.locator(
                'textarea[placeholder*="Введите сообщение"], '
                'input[placeholder*="Введите сообщение"], '
                '[contenteditable="true"][data-placeholder*="Введите сообщение"]'
            )
            for index in range(await fallback.count()):
                candidate = fallback.nth(index)
                if await candidate.is_visible():
                    visible_editor = candidate
                    break
        if visible_editor is None:
            raise RuntimeError("MTS Link не показал поле «Введите сообщение»")
        delivered = page.get_by_text(message, exact=True)
        before = await delivered.count()
        await visible_editor.fill(message)
        await visible_editor.press("Enter")
        for _ in range(10):
            await page.wait_for_timeout(500)
            if await delivered.count() > before:
                log.info("chat_delivery_confirmed")
                return
        raise RuntimeError(
            "Не удалось подтвердить доставку сообщения; повтор отключён во избежание дубля"
        )

    def close(self) -> None:
        """Close only the dedicated browser instance started by this service."""
        if not self.is_running:
            return
        run_async(self._close_async())
        log.info("browser_closed")
        (self.profile_dir / self.PORT_FILE).unlink(missing_ok=True)
        self.port = None
        self.process = None
        self.muted = None

    async def _close_async(self) -> None:

        browser = await self._connected_browser()
        await browser.close()
        await self._release_connection()

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
        from playwright.async_api import Error as PlaywrightError

        for attempt in range(attempts):
            try:
                return await page.content()
            except PlaywrightError:
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
